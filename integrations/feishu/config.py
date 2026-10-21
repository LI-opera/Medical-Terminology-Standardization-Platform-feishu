"""Small local configuration reader; no dotenv auto-discovery or secret output."""

from dataclasses import dataclass
import os
from pathlib import Path
from urllib.parse import urlsplit

DEFAULT_ENV = Path(__file__).with_name('.env')


@dataclass(frozen=True)
class Config:
    app_id: str
    app_secret: str
    tenant: str = ''
    bot_id: str = ''
    users: frozenset[str] = frozenset()
    chats: frozenset[str] = frozenset()
    api_base: str = 'http://127.0.0.1:8000'
    max_chars: int = 4000
    queue_size: int = 10
    pending_replies: int = 50
    queue_wait: int = 300
    medical_timeout: int = 180
    db_path: Path = Path('runtime/state.sqlite3')


def load(path=DEFAULT_ENV, *, discovery=False):
    path = Path(path).resolve()
    values = {}
    if not path.exists():
        raise ValueError('配置文件不存在；先复制 .env.example 为 .env。')
    for line in path.read_text(encoding='utf-8-sig').splitlines():
        line = line.strip()
        if not line or line.startswith('#'):
            continue
        if '=' not in line:
            raise ValueError('配置必须使用 KEY=value 格式。')
        key, value = line.split('=', 1)
        values[key.strip()] = value.strip().strip('\"\'')

    def get(key, default=''):
        return os.environ.get(key, values.get(key, default)).strip()

    def required(key):
        value = get(key)
        if not value or value.startswith(('replace_', 'your_', '这里')):
            raise ValueError(f'请填写 {key}（不会打印其值）。')
        return value

    def number(key, default, upper):
        try:
            value = int(get(key, str(default)))
        except ValueError:
            raise ValueError(f'{key} 必须为整数。') from None
        if not 1 <= value <= upper:
            raise ValueError(f'{key} 超出允许范围 1..{upper}。')
        return value

    def ids(key, prefix):
        result = frozenset(x.strip() for x in get(key).split(',') if x.strip())
        if any(not x.startswith(prefix) or '*' in x for x in result):
            raise ValueError(f'{key} 包含不正确的 ID；不支持通配符。')
        return result

    app_id, secret = required('FEISHU_APP_ID'), required('FEISHU_APP_SECRET')
    if not app_id.startswith('cli_'):
        raise ValueError('FEISHU_APP_ID 应为 cli_ 开头的应用 ID。')
    tenant, bot = get('FEISHU_ALLOWED_TENANT'), get('FEISHU_BOT_OPEN_ID')
    users, chats = ids('FEISHU_ALLOWED_USERS', 'ou_'), ids('FEISHU_ALLOWED_CHATS', 'oc_')
    if not discovery:
        if not tenant or not bot.startswith('ou_') or not users or not chats:
            raise ValueError('运行模式必须填写租户、机器人 open_id、测试用户和测试群允许列表。先执行 discover。')
    base = get('MEDICAL_API_BASE_URL', 'http://127.0.0.1:8000').rstrip('/')
    url = urlsplit(base)
    if url.scheme not in ('http', 'https') or not url.hostname or url.username or url.password or url.query or url.fragment or url.path:
        raise ValueError('MEDICAL_API_BASE_URL 应为 http(s)://主机:端口，不含 /app、凭证或查询参数。')
    state = Path(get('STATE_DB_PATH', 'runtime/state.sqlite3'))
    if not state.is_absolute():
        state = path.parent / state
    return Config(app_id, secret, tenant, bot, users, chats, base,
                  number('MAX_INPUT_CHARS', 4000, 20000),
                  number('MAX_QUEUE_SIZE', 10, 100),
                  number('MAX_PENDING_REPLIES', 50, 500),
                  number('QUEUE_WAIT_SECONDS', 300, 3600),
                  number('MEDICAL_TIMEOUT_SECONDS', 180, 600), state.resolve())
