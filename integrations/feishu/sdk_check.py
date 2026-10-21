"""Explicit OFFLINE installed-SDK compatibility smoke check. Never connect."""

import importlib.metadata
import asyncio
from contextlib import contextmanager
import socket
from unittest.mock import patch


@contextmanager
def offline_guard():
    # Windows asyncio creates its internal self-pipe with a loopback socketpair.
    # Create that OS-local control channel before blocking all network operations.
    # No SDK is imported and no remote connection is made at this point.
    loop = asyncio.new_event_loop()
    asyncio.set_event_loop(loop)
    try:
        with patch.object(socket.socket, 'connect', side_effect=AssertionError('network forbidden')), \
             patch.object(socket.socket, 'connect_ex', side_effect=AssertionError('network forbidden')), \
             patch.object(socket, 'create_connection', side_effect=AssertionError('network forbidden')), \
             patch.object(socket, 'getaddrinfo', side_effect=AssertionError('network forbidden')):
            yield loop
    finally:
        asyncio.set_event_loop(None)
        loop.close()


def main():
    # Imports and builders stay protected; no ws.Client.start or API call is made.
    with offline_guard():
        import lark_oapi as lark
        from lark_oapi.api.im.v1 import ReplyMessageRequest, ReplyMessageRequestBody, P2ImMessageReceiveV1
        from .main import make_handler
        from .tests.test_bridge import event
        client = lark.Client.builder().app_id('cli_offline').app_secret('synthetic').timeout(15).build()
        assert callable(client.im.v1.message.reply)
        request = (ReplyMessageRequest.builder().message_id('om_offline')
                   .request_body(ReplyMessageRequestBody.builder().msg_type('text')
                                 .content('{"text":"offline"}').uuid('offline-uuid').build()).build())
        assert request.body.uuid == 'offline-uuid'
        seen = []
        handler = make_handler(lark, seen.append)
        # Event handler construction and actual SDK event serialization shape.
        assert handler is not None
        data = P2ImMessageReceiveV1(event())
        import json
        assert json.loads(lark.JSON.marshal(data))['event']['sender']['sender_id']['open_id'] == 'ou_user'
        assert callable(lark.ws.Client)
        print('OFFLINE SDK CHECK PASS:', importlib.metadata.version('lark-oapi'))


if __name__ == '__main__':
    main()
