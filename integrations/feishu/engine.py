"""Event callback never performs model or message API requests."""

import logging
import threading
import uuid

from .clients import AnalysisError, SendError
from .events import parse
from .formatter import render

LOG = logging.getLogger('feishu_bridge')


class Engine:
    def __init__(self, cfg, store, medical, sender):
        self.cfg, self.store, self.medical, self.sender = cfg, store, medical, sender
        self.stopping = threading.Event()
        self.threads = []

    def receive(self, raw):
        if self.stopping.is_set():
            return 'stopping'
        msg = parse(raw, self.cfg)
        if msg is None:
            return 'ignored'
        result = self.store.accept(msg, self.cfg)
        LOG.info('receive status=%s', result)
        return result

    def analyze_once(self):
        job = self.store.claim_job(self.cfg.queue_wait)
        if not job:
            return False
        try:
            text = render(self.medical.analyze(job['body'], job['id']))
            self.store.finish(job['id'], text)
        except AnalysisError as exc:
            state = 'OUTCOME_UNKNOWN' if exc.unknown else 'FAILED'
            text = ('无法确认后端是否完成，本次不会自动重复分析。请凭任务编号联系维护者。'
                    if exc.unknown else '分析接口未返回可用结果，请凭任务编号联系维护者。')
            self.store.finish(job['id'], text, state, exc.code)
        except Exception:
            self.store.finish(job['id'], '分析结果格式异常或处理失败，请凭任务编号联系维护者；不会自动重复分析。', 'FAILED', 'processing_error')
        LOG.info('analysis finished job=%s', job['id'])
        return True

    def send_once(self):
        row = self.store.claim_reply()
        if not row:
            return False
        delivery_id = str(uuid.uuid5(uuid.NAMESPACE_URL, f"{row['job_id']}:{row['part']}"))
        try:
            self.sender.reply(row['message_id'], row['body'], delivery_id)
        except SendError as exc:
            delay = None if exc.retry_after is None else exc.retry_after * (2 ** (row['attempts'] - 1))
            self.store.reply_failed(row, exc.code, delay)
            LOG.warning('reply failed job=%s code=%s', row['job_id'], exc.code)
        except Exception:
            self.store.reply_failed(row, 'send_internal_error')
            LOG.error('reply internal error job=%s', row['job_id'])
        else:
            self.store.sent(row)
        return True

    def _work(self):
        while not self.stopping.is_set():
            try:
                self.store.cleanup()
                if not self.analyze_once():
                    self.stopping.wait(.5)
            except Exception:
                LOG.error('worker storage failure; stopping, restart requires inspection')
                self.stopping.set()

    def _send(self):
        while not self.stopping.is_set():
            try:
                self.send_once()
            except Exception:
                LOG.error('sender storage failure; stopping, restart requires inspection')
                self.stopping.set()
            # Globally < 5 messages/s including help, chunks and retries.
            self.stopping.wait(.3)

    def start(self):
        self.store.recover()
        self.store.cleanup()
        self.threads = [threading.Thread(target=f, daemon=True) for f in (self._work, self._send)]
        for thread in self.threads:
            thread.start()

    def stop(self):
        self.stopping.set()
        for thread in self.threads:
            thread.join(timeout=self.cfg.medical_timeout + 20)
        return not any(t.is_alive() for t in self.threads)
