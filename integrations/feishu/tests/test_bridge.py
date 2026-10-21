import copy
from dataclasses import replace
import io
import json
from pathlib import Path
import socket
import tempfile
import threading
from types import ModuleType, SimpleNamespace
import unittest
from unittest.mock import patch
from urllib.error import HTTPError

from integrations.feishu.clients import AnalysisError, FeishuSender, MedicalClient, SendError
from integrations.feishu.config import Config, load
from integrations.feishu.engine import Engine
from integrations.feishu.events import discovery_record, parse
from integrations.feishu.formatter import chunks, render
from integrations.feishu.main import instance_lock, main
from integrations.feishu.store import Store


def event(mid='om_1', user='ou_user', chat='oc_group', kind='p2p', text='SOB and CP.', mentions=None, mtype='text'):
    return {'header': {'app_id': 'cli_test', 'tenant_key': 'tenant_test', 'event_type': 'im.message.receive_v1'},
            'event': {'sender': {'sender_type': 'user', 'sender_id': {'open_id': user}, 'tenant_key': 'tenant_test'},
                      'message': {'message_id': mid, 'chat_id': chat, 'chat_type': kind,
                                  'message_type': mtype, 'content': json.dumps({'text': text}),
                                  'mentions': mentions or []}}}


OWN = [{'key': '@_user_1', 'id': {'open_id': 'ou_bot'}}]
RESULT = {'request_id': 'ana_test', 'success': False, 'expanded_text': 'shortness of breath',
          'mapping_states': [{'abbreviation': 'SOB', 'expansion': 'shortness of breath', 'status': 'WITHHELD',
                              'chosen_concept': {'concept_code': 'SHOULD_NOT_SHOW'}, 'failure': {'type': 'NO_RELIABLE_CODE'}}]}


class Medical:
    def __init__(self):
        self.calls = []
        self.error = None

    def analyze(self, text, job):
        self.calls.append((text, job))
        if self.error:
            raise self.error
        return copy.deepcopy(RESULT)


class Sender:
    def __init__(self):
        self.calls = []
        self.error = None

    def reply(self, mid, text, uid):
        self.calls.append((mid, text, uid))
        if self.error:
            raise self.error


class OfflineTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.guards = [patch.object(socket, 'socket', side_effect=AssertionError('NETWORK FORBIDDEN')),
                      patch.object(socket, 'create_connection', side_effect=AssertionError('NETWORK FORBIDDEN')),
                      patch.object(socket, 'getaddrinfo', side_effect=AssertionError('NETWORK FORBIDDEN'))]
        for guard in cls.guards:
            guard.start()

    @classmethod
    def tearDownClass(cls):
        for guard in reversed(cls.guards):
            guard.stop()

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.now = 1000000.0
        self.cfg = Config('cli_test', 'synthetic_secret', 'tenant_test', 'ou_bot',
                          frozenset(['ou_user', 'ou_other']), frozenset(['oc_group', 'oc_other']),
                          db_path=Path(self.tmp.name) / 'state.db')
        self.store = Store(self.cfg.db_path, lambda: self.now)
        self.medical, self.sender = Medical(), Sender()
        self.engine = Engine(self.cfg, self.store, self.medical, self.sender)

    def tearDown(self):
        self.store.close()
        self.tmp.cleanup()

    def drain(self):
        for _ in range(30):
            if not self.engine.send_once():
                break

    def test_group_requires_real_own_mention_and_both_allowlists(self):
        self.assertIsNone(parse(event(kind='group', text='@医学文本标准化助手 SOB'), self.cfg))
        other = [{'key': '@_user_1', 'id': {'open_id': 'ou_else'}}]
        self.assertIsNone(parse(event(kind='group', text='@_user_1 SOB', mentions=other), self.cfg))
        good = event(kind='group', text='@_user_1 SOB', mentions=OWN)
        self.assertEqual(parse(good, self.cfg).text, 'SOB')
        self.assertIsNone(parse(event(kind='group', text='@_user_1 SOB', mentions=OWN, chat='oc_no'), self.cfg))
        self.assertIsNone(parse(event(kind='group', text='@_user_1 SOB', mentions=OWN, user='ou_no'), self.cfg))
        self.assertIsNone(parse(event(kind='group', text='SOB', mentions=OWN), self.cfg))

    def test_app_tenant_sender_and_malformed_events_rejected(self):
        for field, val in [('app_id', 'cli_wrong'), ('tenant_key', 'wrong'), ('event_type', 'wrong')]:
            raw = event()
            raw['header'][field] = val
            self.assertIsNone(parse(raw, self.cfg))
        raw = event()
        raw['event']['sender']['sender_type'] = 'app'
        self.assertIsNone(parse(raw, self.cfg))
        self.assertIsNone(parse(None, self.cfg))
        self.assertIsNone(parse(event(user='ou_bot'), self.cfg))

    def test_help_and_invalid_input_do_not_call_medical(self):
        for i, raw in enumerate([event(text='帮助'), event(text=''), event(text='仅中文'),
                                 event(text='a' * 4001), event(mtype='image'),
                                 event(kind='group', text='@_user_1', mentions=OWN)]):
            raw['event']['message']['message_id'] = f'om_{i}'
            self.engine.receive(raw)
        self.assertFalse(self.engine.analyze_once())
        self.drain()
        self.assertEqual(len(self.sender.calls), 6)
        self.assertEqual(self.medical.calls, [])

    def test_concurrent_redelivery_executes_once(self):
        threads = [threading.Thread(target=self.engine.receive, args=(event(),)) for _ in range(12)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()
        self.assertTrue(self.engine.analyze_once())
        self.assertFalse(self.engine.analyze_once())
        self.assertEqual(len(self.medical.calls), 1)
        self.assertEqual(sum(self.store.summary().values()), 1)

    def test_bounded_queue_and_one_active_per_user(self):
        self.engine.cfg = replace(self.cfg, queue_size=1)
        self.engine.receive(event())
        self.engine.receive(event(mid='om_2'))
        self.engine.receive(event(mid='om_3', user='ou_other'))
        self.assertEqual(self.store.summary()['QUEUED'], 1)
        self.drain()
        self.assertTrue(any('已有' in c[1] for c in self.sender.calls))
        self.assertTrue(any('已满' in c[1] for c in self.sender.calls))

    def test_reply_backlog_is_bounded(self):
        self.engine.cfg = replace(self.cfg, pending_replies=1)
        self.engine.receive(event(text='帮助'))
        self.assertEqual(self.engine.receive(event(mid='om_2', text='帮助')), 'reply_backlog')
        self.assertEqual(sum(self.store.summary().values()), 1)

    def test_result_is_faithful_and_routes_do_not_mix(self):
        self.engine.receive(event(mid='om_private'))
        self.engine.receive(event(mid='om_group', user='ou_other', kind='group', chat='oc_other', text='@_user_1 CP', mentions=OWN))
        self.engine.analyze_once()
        self.engine.analyze_once()
        self.drain()
        self.assertEqual({c[0] for c in self.sender.calls}, {'om_private', 'om_group'})
        finals = [c for c in self.sender.calls if '逐项结果' in c[1]]
        self.assertEqual(len(finals), 2)
        for c in finals:
            self.assertIn('WITHHELD', c[1])
            self.assertNotIn('SHOULD_NOT_SHOW', c[1])

    def test_timeout_is_unknown_and_not_retried(self):
        self.medical.error = AnalysisError('network_or_timeout', unknown=True)
        self.engine.receive(event())
        self.engine.analyze_once()
        self.engine.analyze_once()
        self.store.recover()
        self.assertFalse(self.engine.analyze_once())
        self.assertEqual(len(self.medical.calls), 1)
        self.assertEqual(self.store.summary()['OUTCOME_UNKNOWN'], 1)

    def test_send_retries_same_uuid_without_reanalysis(self):
        self.engine.receive(event())
        self.engine.analyze_once()
        self.sender.error = SendError('send_rate_limited', 2)
        for _ in range(3):
            self.engine.send_once()
            self.now += 100
        ids = [c[2] for c in self.sender.calls]
        self.assertEqual(len(set(ids)), 1)
        self.sender.error = None
        self.drain()
        self.assertEqual(len(self.medical.calls), 1)

    def test_permanent_send_failure_and_result_retention(self):
        self.engine.receive(event(text='帮助'))
        self.sender.error = SendError('permission_denied')
        self.drain()
        self.assertEqual(len(self.sender.calls), 1)
        self.assertEqual(self.store.summary()['DELIVERY_FAILED'], 1)
        self.now += 86401
        self.store.cleanup()
        self.assertEqual(self.store.db.execute('SELECT body FROM outbox').fetchone()[0], '')

    def test_restart_marks_inflight_unknown_but_keeps_queued(self):
        self.engine.receive(event())
        self.engine.receive(event(mid='om_other', user='ou_other'))
        self.store.claim_job(300)
        self.store.close()
        self.store = Store(self.cfg.db_path, lambda: self.now)
        self.engine.store = self.store
        self.store.recover()
        self.assertEqual(self.store.summary()['OUTCOME_UNKNOWN'], 1)
        self.assertEqual(self.store.summary()['QUEUED'], 1)
        self.assertEqual(self.engine.receive(event()), 'duplicate')
        self.engine.analyze_once()
        self.assertEqual(len(self.medical.calls), 1)

    def test_restart_completed_result_sends_without_reanalysis(self):
        self.engine.receive(event())
        self.engine.analyze_once()
        self.store.claim_reply()  # interrupted after claim, before confirmation
        self.store.recover()
        self.assertFalse(self.engine.analyze_once())
        self.drain()
        self.assertEqual(self.store.summary()['DELIVERED'], 1)

    def test_queue_expiry_never_analyzes(self):
        self.engine.receive(event())
        self.now += 301
        self.assertFalse(self.engine.analyze_once())
        self.assertEqual(self.medical.calls, [])

    def test_payloads_erased_after_delivery_and_metadata_expires(self):
        self.engine.receive(event())
        self.engine.analyze_once()
        self.drain()
        self.assertEqual(self.store.db.execute('SELECT body FROM jobs').fetchone()[0], '')
        self.assertTrue(all(r[0] == '' for r in self.store.db.execute('SELECT body FROM outbox')))
        self.now += 7 * 86400 + 1
        self.store.cleanup()
        self.assertEqual(self.store.summary(), {})

    def test_all_business_states_and_no_target(self):
        data = copy.deepcopy(RESULT)
        data['mapping_states'] = [{'abbreviation': 'X', 'status': status} for status in
                                  ['CODED', 'WITHHELD', 'NOT_EXPANDED', 'ABSTAIN', 'PENDING', 'NEW_STATUS']]
        output = render(data)
        self.assertIn('缺少概念详情', output)
        self.assertIn('状态未识别', output)
        self.assertIn('ABSTAIN', output)
        data['mapping_states'] = []
        self.assertIn('未识别到', render(data))

    def test_formatter_escapes_mentions_and_utf8_chunks_reassemble(self):
        data = copy.deepcopy(RESULT)
        data['expanded_text'] = '<at user_id="all">' + '中' * 5000
        text = render(data)
        self.assertNotIn('<at', text)
        pieces = chunks(text, 'id')
        self.assertGreater(len(pieces), 1)
        self.assertEqual(''.join(p.split('\n', 1)[1] for p in pieces), text)
        self.assertTrue(all(len(json.dumps({'text': p}, ensure_ascii=False).encode()) < 4000 for p in pieces))

    def test_discovery_nonce_only_prints_ids(self):
        raw = event(kind='group', text='@_user_1 /register secret_nonce', mentions=OWN)
        self.assertIsNone(discovery_record(raw, 'cli_test', 'wrong'))
        record = discovery_record(raw, 'cli_test', 'secret_nonce')
        self.assertEqual(record['mentioned_open_ids'], ['ou_bot'])
        self.assertNotIn('secret_nonce', json.dumps(record))
        self.assertNotIn('content', record)

    def test_medical_request_contract_mocked_no_network(self):
        class Opener:
            def open(inner, request, timeout):
                self.assertEqual(request.full_url, 'http://127.0.0.1:8000/expand/simple')
                self.assertEqual(json.loads(request.data), {'text': 'SOB'})
                self.assertEqual(request.get_method(), 'POST')
                self.assertEqual(timeout, 180)
                return io.BytesIO(json.dumps(RESULT).encode())
        self.assertEqual(MedicalClient(self.cfg, Opener()).analyze('SOB', 'job'), RESULT)

    def test_medical_errors_are_sanitized_no_retry(self):
        class Opener:
            def open(inner, *args, **kwargs):
                raise TimeoutError('SECRET CLINICAL BODY')
        with self.assertRaises(AnalysisError) as ctx:
            MedicalClient(self.cfg, Opener()).analyze('SOB', 'job')
        self.assertTrue(ctx.exception.unknown)
        self.assertNotIn('SECRET', str(ctx.exception))
        class BadHTTP:
            def open(inner, *args, **kwargs):
                raise HTTPError('http://test', 503, 'SECRET', {}, None)
        with self.assertRaises(AnalysisError) as ctx:
            MedicalClient(self.cfg, BadHTTP()).analyze('SOB', 'job')
        self.assertTrue(ctx.exception.unknown)

    def test_config_fail_closed_and_check_never_connects(self):
        p = Path(self.tmp.name) / '.env'
        p.write_text('FEISHU_APP_ID=cli_test\nFEISHU_APP_SECRET=synthetic_secret\n', encoding='utf-8')
        with patch.dict('os.environ', {}, clear=True):
            with self.assertRaises(ValueError):
                load(p)
            self.assertEqual(load(p, discovery=True).app_id, 'cli_test')
            with patch('sys.stdout', new_callable=io.StringIO) as out:
                self.assertEqual(main(['check', '--discovery', '--env', str(p)]), 0)
                self.assertNotIn('synthetic_secret', out.getvalue())

    def test_single_instance_lock(self):
        p = Path(self.tmp.name) / 'lock'
        with instance_lock(p):
            with self.assertRaises(ValueError):
                with instance_lock(p):
                    self.fail('second lock should fail')
        with instance_lock(p):
            pass

    def test_sdk_reply_adapter_builds_correct_request_and_classifies_errors(self):
        # Synthetic SDK builder only. Actual installed SDK compatibility is a separate check.
        class Builder:
            @staticmethod
            def builder():
                return Builder()

            def __getattr__(self, key):
                def setter(value):
                    self.__dict__[key] = value
                    return self
                return setter

            def build(self):
                return self

        mod = ModuleType('lark_oapi.api.im.v1')
        mod.ReplyMessageRequest = mod.ReplyMessageRequestBody = Builder
        response = SimpleNamespace(success=lambda: True)
        requests = []

        def reply(request):
            requests.append(request)
            return response

        client = SimpleNamespace(im=SimpleNamespace(v1=SimpleNamespace(message=SimpleNamespace(reply=reply))))
        with patch.dict('sys.modules', {'lark_oapi.api.im.v1': mod}):
            adapter = FeishuSender(client)
            adapter.reply('om_origin', '中文结果', 'stable-delivery-id')
            req = requests[0]
            self.assertEqual(req.message_id, 'om_origin')
            self.assertEqual(req.request_body.uuid, 'stable-delivery-id')
            self.assertEqual(req.request_body.msg_type, 'text')
            self.assertEqual(json.loads(req.request_body.content), {'text': '中文结果'})
            response = SimpleNamespace(success=lambda: False, code=99991400,
                                       raw=SimpleNamespace(status_code=429, headers={'Retry-After': '10'}))
            with self.assertRaises(SendError) as ctx:
                adapter.reply('om_origin', 'text', 'same-id')
            self.assertEqual(ctx.exception.retry_after, 10)
            response = SimpleNamespace(success=lambda: False, code=230002,
                                       raw=SimpleNamespace(status_code=400, headers={}))
            with self.assertRaises(SendError) as ctx:
                adapter.reply('om_origin', 'text', 'same-id')
            self.assertIsNone(ctx.exception.retry_after)

    def test_config_full_validation_and_relative_state_path(self):
        p = Path(self.tmp.name) / '.env'
        content = ('FEISHU_APP_ID=cli_test\nFEISHU_APP_SECRET=synthetic\n'
                   'FEISHU_ALLOWED_TENANT=tenant_test\nFEISHU_BOT_OPEN_ID=ou_bot\n'
                   'FEISHU_ALLOWED_USERS=ou_user,ou_other\nFEISHU_ALLOWED_CHATS=oc_group\n'
                   'STATE_DB_PATH=runtime/state.sqlite3\n')
        with patch.dict('os.environ', {}, clear=True):
            p.write_text(content, encoding='utf-8')
            self.assertEqual(load(p).db_path, (p.parent / 'runtime/state.sqlite3').resolve())
            for extra in ['MEDICAL_API_BASE_URL=http://host/app\n', 'MAX_QUEUE_SIZE=0\n',
                          'FEISHU_ALLOWED_USERS=*\n', 'MEDICAL_API_BASE_URL=http://user:pass@host\n']:
                p.write_text(content + extra, encoding='utf-8')
                with self.assertRaises(ValueError):
                    load(p)

    def test_background_threads_process_and_stop(self):
        import time
        self.engine.start()
        try:
            self.engine.receive(event())
            end = time.monotonic() + 3
            while time.monotonic() < end and self.store.summary().get('DELIVERED') != 1:
                time.sleep(.02)
            self.assertEqual(self.store.summary().get('DELIVERED'), 1)
        finally:
            self.assertTrue(self.engine.stop())
        self.assertEqual(self.engine.receive(event(mid='om_stopped')), 'stopping')


if __name__ == '__main__':
    unittest.main()
