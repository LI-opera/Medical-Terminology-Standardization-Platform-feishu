import asyncio
import importlib.util
import socket
import unittest

from integrations.feishu.sdk_check import offline_guard, main


class OfflineSDKTests(unittest.TestCase):
    def test_loop_initialized_but_network_operations_still_blocked(self):
        with offline_guard() as loop:
            self.assertEqual(loop.run_until_complete(asyncio.sleep(0, result='ok')), 'ok')
            with socket.socket() as sock:
                # Even direct loopback access is forbidden once the guard is active.
                for address in [('127.0.0.1', 9), ('192.0.2.1', 443)]:
                    with self.assertRaisesRegex(AssertionError, 'network forbidden'):
                        sock.connect(address)
                    with self.assertRaisesRegex(AssertionError, 'network forbidden'):
                        sock.connect_ex(address)
            with self.assertRaisesRegex(AssertionError, 'network forbidden'):
                socket.getaddrinfo('example.invalid', 443)
            with self.assertRaisesRegex(AssertionError, 'network forbidden'):
                socket.create_connection(('192.0.2.1', 443))
        self.assertTrue(loop.is_closed())

    @unittest.skipUnless(importlib.util.find_spec('lark_oapi'), 'SDK not installed')
    def test_installed_sdk_builders_and_event_serialization_offline(self):
        main()
