"""Run: python -m integrations.feishu.main check|discover|run|status."""

import argparse
from contextlib import contextmanager
import importlib.util
import json
import logging
import os
from pathlib import Path
import secrets
import sqlite3
import sys
import time

from .config import DEFAULT_ENV, load
from .events import discovery_record


@contextmanager
def instance_lock(path):
    """OS lock released even on process death; file existence is not the lock."""
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    handle = path.open('a+b')
    handle.seek(0, 2)
    if handle.tell() == 0:
        handle.write(b'0')
        handle.flush()
    handle.seek(0)
    try:
        try:
            if os.name == 'nt':
                import msvcrt
                msvcrt.locking(handle.fileno(), msvcrt.LK_NBLCK, 1)
            else:
                import fcntl
                fcntl.flock(handle, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError:
            raise ValueError('此状态库已有接入进程运行，请先停止旧进程。') from None
        yield
    finally:
        handle.close()


def make_handler(lark, callback):
    def on_message(data):
        callback(json.loads(lark.JSON.marshal(data)))
    return (lark.EventDispatcherHandler.builder('', '')
            .register_p2_im_message_receive_v1(on_message).build())


def connect(cfg, mode):
    # This is the ONLY entry that loads the SDK or opens its WebSocket.
    import lark_oapi as lark
    from lark_oapi.core.log import logger as sdk_logger
    from .clients import FeishuSender, MedicalClient
    from .engine import Engine
    from .store import Store

    # SDK errors may log event bodies or server responses. Our own logs are allowlisted.
    sdk_logger.disabled = True
    engine = store = None
    if mode == 'discover':
        token = secrets.token_hex(8)
        deadline = time.monotonic() + 600
        print('登记模式：只在本机显示匹配口令消息的身份 ID；不回复、不分析。')
        print(f'请在机器人单聊发送：/register {token}')
        print(f'再在测试群真正 @ 此机器人，后面发送：/register {token}')
        print('仅 @ 此机器人，不要同时 @ 他人。口令 10 分钟后失效；完成后 Ctrl+C 停止。')

        def callback(raw):
            if time.monotonic() > deadline:
                return
            record = discovery_record(raw, cfg.app_id, token)
            if record:
                print(json.dumps(record, ensure_ascii=True), flush=True)
    else:
        client = (lark.Client.builder().app_id(cfg.app_id).app_secret(cfg.app_secret)
                  .timeout(15).log_level(lark.LogLevel.ERROR).build())
        store = Store(cfg.db_path)
        engine = Engine(cfg, store, MedicalClient(cfg), FeishuSender(client))
        callback = engine.receive

    ws = lark.ws.Client(cfg.app_id, cfg.app_secret,
                        event_handler=make_handler(lark, callback), log_level=lark.LogLevel.ERROR)
    try:
        if engine:
            engine.start()
        print('正在尝试连接飞书（此行不代表连接已成功）。Ctrl+C 停止。', flush=True)
        ws.start()
    finally:
        if engine:
            print('正在停止接收并等待当前请求结束；尚未发送的结果将在下次启动继续投递。', flush=True)
            if engine.stop():
                store.close()


def main(argv=None):
    parser = argparse.ArgumentParser(description='Medical NLP 飞书接入；check/status 不联网。')
    parser.add_argument('command', choices=['check', 'discover', 'run', 'status'])
    parser.add_argument('--env', type=Path, default=DEFAULT_ENV)
    parser.add_argument('--discovery', action='store_true', help='check 时只检查登记模式所需配置')
    args = parser.parse_args(argv)
    logging.basicConfig(level=logging.INFO, format='%(asctime)s %(levelname)s %(message)s')
    try:
        cfg = load(args.env, discovery=args.command in ('discover', 'status') or args.discovery)
        if args.command == 'check':
            if args.discovery:
                print('登记配置检查通过。未联网，未验证凭证有效性。')
            else:
                print(f'运行配置检查通过：允许用户 {len(cfg.users)}，允许群 {len(cfg.chats)}。未联网。')
            print('SDK 已安装。' if importlib.util.find_spec('lark_oapi') else 'SDK 未安装；check 可执行，连接前需按 README 安装依赖。')
        elif args.command == 'status':
            if not cfg.db_path.exists():
                print('尚无本机任务记录。')
            else:
                with sqlite3.connect(cfg.db_path.as_uri() + '?mode=ro', uri=True) as db:
                    print('任务状态：', dict(db.execute('SELECT state,COUNT(*) FROM jobs GROUP BY state')))
                    print('投递状态：', dict(db.execute('SELECT state,COUNT(*) FROM outbox GROUP BY state')))
        else:
            with instance_lock(str(cfg.db_path) + '.lock'):
                connect(cfg, args.command)
        return 0
    except KeyboardInterrupt:
        print('已停止。')
        return 0
    except ValueError as exc:
        # Only config/lock ValueErrors are safe. No raw SDK/network exception detail.
        if args.command in ('check', 'status'):
            print(str(exc), file=sys.stderr)
        else:
            print('启动失败：检查配置、重复进程和 SDK 兼容性；先执行 check。', file=sys.stderr)
        return 2
    except ModuleNotFoundError:
        print('缺少接入依赖，请使用独立环境安装 requirements.txt。', file=sys.stderr)
        return 2
    except Exception as exc:
        print(f'接入失败（{type(exc).__name__}）；检查权限/发布/网络。未输出原始错误以保护凭证和消息。', file=sys.stderr)
        return 2


if __name__ == '__main__':
    raise SystemExit(main())
