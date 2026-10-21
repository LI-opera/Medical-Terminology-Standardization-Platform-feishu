"""Single-instance durable queue/outbox. Transactions protect concurrent callbacks."""

import json
from pathlib import Path
import sqlite3
import threading
import time
import uuid

from .formatter import chunks


class Store:
    def __init__(self, path, clock=time.time):
        Path(path).parent.mkdir(parents=True, exist_ok=True)
        self.clock, self.lock = clock, threading.RLock()
        self.db = sqlite3.connect(path, check_same_thread=False, timeout=1)
        self.db.row_factory = sqlite3.Row
        self.db.execute('PRAGMA journal_mode=WAL')
        self.db.execute('PRAGMA secure_delete=ON')
        self.db.executescript('''
            CREATE TABLE IF NOT EXISTS jobs (
                id TEXT PRIMARY KEY, dedup TEXT UNIQUE, message_id TEXT,
                chat_id TEXT, user_id TEXT, body TEXT, state TEXT,
                created REAL, updated REAL, error TEXT DEFAULT '');
            CREATE TABLE IF NOT EXISTS outbox (
                job_id TEXT, part INTEGER, body TEXT, state TEXT,
                attempts INTEGER DEFAULT 0, next_at REAL DEFAULT 0,
                error TEXT DEFAULT '', PRIMARY KEY(job_id,part));
        ''')

    def _out(self, job, part, body):
        self.db.execute('INSERT OR IGNORE INTO outbox(job_id,part,body,state) VALUES(?,?,?,?)',
                        (job, part, body, 'PENDING'))

    def _finish(self, job, state, text, error=''):
        self.db.execute('UPDATE jobs SET state=?,body="",updated=?,error=? WHERE id=?',
                        (state, self.clock(), error, job))
        for i, part in enumerate(chunks(text, job), 1):
            self._out(job, i, part)

    def accept(self, msg, cfg):
        now = self.clock()
        key = json.dumps([cfg.app_id, msg.tenant, msg.message_id])
        with self.lock, self.db:
            if self.db.execute('SELECT 1 FROM jobs WHERE dedup=?', (key,)).fetchone():
                return 'duplicate'
            pending = self.db.execute("SELECT COUNT(DISTINCT job_id) FROM outbox WHERE state IN ('PENDING','SENDING')").fetchone()[0]
            if pending >= cfg.pending_replies:
                # Hard bound even on help/rejection messages during an outage.
                return 'reply_backlog'
            notice = msg.notice
            if not notice:
                waiting = self.db.execute("SELECT COUNT(*) FROM jobs WHERE state='QUEUED'").fetchone()[0]
                busy = self.db.execute("SELECT 1 FROM jobs WHERE user_id=? AND state IN ('QUEUED','RUNNING')", (msg.user_id,)).fetchone()
                if busy:
                    notice = '你已有排队或处理中的任务，请等待结果后再提交。'
                elif waiting >= cfg.queue_size:
                    notice = '等待队列已满，本次未启动分析，请稍后再试。'
            job = uuid.uuid4().hex[:16]
            self.db.execute('INSERT INTO jobs(id,dedup,message_id,chat_id,user_id,body,state,created,updated) VALUES(?,?,?,?,?,?,?,?,?)',
                            (job, key, msg.message_id, msg.chat_id, msg.user_id,
                             '' if notice else msg.text, 'RESULT_READY' if notice else 'QUEUED', now, now))
            if notice:
                self._finish(job, 'RESULT_READY', notice)
            else:
                self._out(job, 0, f'任务 {job}：已收到，正在排队/处理。')
            return job

    def claim_job(self, wait_seconds):
        with self.lock, self.db:
            row = self.db.execute("SELECT * FROM jobs WHERE state='QUEUED' ORDER BY created,id LIMIT 1").fetchone()
            if not row:
                return None
            if self.clock() - row['created'] > wait_seconds:
                self._finish(row['id'], 'FAILED', '排队等待已超时，本次未启动分析。', 'queue_expired')
                return None
            self.db.execute("UPDATE jobs SET state='RUNNING',updated=? WHERE id=?", (self.clock(), row['id']))
            return dict(row)

    def finish(self, job, text, state='RESULT_READY', error=''):
        with self.lock, self.db:
            self._finish(job, state, text, error)

    def claim_reply(self):
        with self.lock, self.db:
            row = self.db.execute('''SELECT o.*, j.message_id FROM outbox o JOIN jobs j ON j.id=o.job_id
                WHERE o.state='PENDING' AND o.next_at<=?
                AND NOT EXISTS (SELECT 1 FROM outbox p WHERE p.job_id=o.job_id
                    AND p.part<o.part AND p.state IN ('PENDING','SENDING'))
                ORDER BY j.created,o.part LIMIT 1''', (self.clock(),)).fetchone()
            if not row:
                return None
            self.db.execute("UPDATE outbox SET state='SENDING',attempts=attempts+1 WHERE job_id=? AND part=?",
                            (row['job_id'], row['part']))
            result = dict(row)
            result['attempts'] += 1
            return result

    def sent(self, row):
        with self.lock, self.db:
            self.db.execute("UPDATE outbox SET state='SENT',body='' WHERE job_id=? AND part=?", (row['job_id'], row['part']))
            self._settle(row['job_id'])

    def _settle(self, job):
        outstanding = self.db.execute("SELECT 1 FROM outbox WHERE job_id=? AND state IN ('PENDING','SENDING')", (job,)).fetchone()
        if outstanding:
            return
        failed = self.db.execute("SELECT 1 FROM outbox WHERE job_id=? AND part>0 AND state='FAILED'", (job,)).fetchone()
        self.db.execute("UPDATE jobs SET state=?,updated=? WHERE id=? AND state='RESULT_READY'",
                        ('DELIVERY_FAILED' if failed else 'DELIVERED', self.clock(), job))

    def reply_failed(self, row, error, retry_after=None):
        with self.lock, self.db:
            if retry_after is not None and row['attempts'] < 3:
                self.db.execute("UPDATE outbox SET state='PENDING',next_at=?,error=? WHERE job_id=? AND part=?",
                                (self.clock() + retry_after, error, row['job_id'], row['part']))
            else:
                self.db.execute("UPDATE outbox SET state='FAILED',error=? WHERE job_id=? AND part=?",
                                (error, row['job_id'], row['part']))
            self._settle(row['job_id'])

    def recover(self):
        with self.lock, self.db:
            for row in self.db.execute("SELECT id FROM jobs WHERE state='RUNNING'").fetchall():
                self._finish(row['id'], 'OUTCOME_UNKNOWN', '接入程序曾中断，无法确认后端是否完成；不会自动重复分析。', 'interrupted')
            self.db.execute("UPDATE outbox SET state='PENDING' WHERE state='SENDING' AND attempts<3")
            self.db.execute("UPDATE outbox SET state='FAILED',error='interrupted_send' WHERE state='SENDING'")
            for row in self.db.execute("SELECT id FROM jobs WHERE state='RESULT_READY'").fetchall():
                self._settle(row['id'])

    def cleanup(self):
        with self.lock, self.db:
            now = self.clock()
            # Do not interrupt a current analysis; its request has a bounded client timeout.
            old = [r['id'] for r in self.db.execute("SELECT id FROM jobs WHERE created<? AND state!='RUNNING'", (now - 86400,))]
            for job in old:
                self.db.execute("UPDATE jobs SET body='',state=CASE WHEN state IN ('QUEUED','RESULT_READY') THEN 'EXPIRED' ELSE state END WHERE id=?", (job,))
                self.db.execute("UPDATE outbox SET body='',state=CASE WHEN state IN ('PENDING','SENDING') THEN 'FAILED' ELSE state END WHERE job_id=?", (job,))
            self.db.execute('DELETE FROM outbox WHERE job_id IN (SELECT id FROM jobs WHERE created<?)', (now - 7 * 86400,))
            self.db.execute('DELETE FROM jobs WHERE created<?', (now - 7 * 86400,))

    def summary(self):
        with self.lock:
            return {r[0]: r[1] for r in self.db.execute('SELECT state,COUNT(*) FROM jobs GROUP BY state')}

    def close(self):
        with self.lock:
            self.db.execute('PRAGMA wal_checkpoint(TRUNCATE)')
            self.db.close()
