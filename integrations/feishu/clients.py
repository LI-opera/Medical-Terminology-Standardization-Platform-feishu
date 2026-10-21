"""Network adapters loaded/used only by the explicit run command."""

import json
import socket
from urllib.error import HTTPError, URLError
from urllib.request import Request, build_opener, HTTPRedirectHandler, ProxyHandler


class AnalysisError(Exception):
    def __init__(self, code, unknown=False):
        super().__init__(code)
        self.code, self.unknown = code, unknown


class SendError(Exception):
    def __init__(self, code, retry_after=None):
        super().__init__(code)
        self.code, self.retry_after = code, retry_after


class NoRedirect(HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


class MedicalClient:
    def __init__(self, cfg, opener=None):
        self.cfg = cfg
        # Don't route local clinical input via environment proxies or redirects.
        self.opener = opener or build_opener(ProxyHandler({}), NoRedirect())

    def analyze(self, text, job_id):
        request = Request(self.cfg.api_base + '/expand/simple',
                          data=json.dumps({'text': text}).encode('utf-8'),
                          headers={'Content-Type': 'application/json', 'X-Frontend-Request-Id': 'feishu_' + job_id}, method='POST')
        try:
            with self.opener.open(request, timeout=self.cfg.medical_timeout) as response:
                data = response.read(2_000_001)
            if len(data) > 2_000_000:
                raise AnalysisError('response_too_large')
            return json.loads(data)
        except HTTPError as exc:
            raise AnalysisError(f'http_{exc.code}', unknown=exc.code >= 500) from None
        except (TimeoutError, socket.timeout, URLError, OSError):
            raise AnalysisError('network_or_timeout', unknown=True) from None
        except (ValueError, UnicodeError):
            raise AnalysisError('invalid_json') from None


class FeishuSender:
    def __init__(self, client):
        self.client = client

    def reply(self, message_id, text, delivery_id):
        from lark_oapi.api.im.v1 import ReplyMessageRequest, ReplyMessageRequestBody
        request = (ReplyMessageRequest.builder().message_id(message_id)
                   .request_body(ReplyMessageRequestBody.builder().msg_type('text')
                                 .content(json.dumps({'text': text}, ensure_ascii=False))
                                 .uuid(delivery_id).build()).build())
        try:
            response = self.client.im.v1.message.reply(request)
        except Exception:
            # No exception text: SDK errors may contain content or credentials.
            raise SendError('send_network_unknown', retry_after=2) from None
        if response.success():
            return
        code = response.code
        raw = getattr(response, 'raw', None)
        http_status = getattr(raw, 'status_code', 0)
        if code in (99991400, 230020) or http_status == 429:
            delay = 5
            headers = getattr(raw, 'headers', {}) or {}
            try:
                delay = min(300, max(1, int(headers.get('Retry-After', headers.get('retry-after', 5)))))
            except (ValueError, TypeError):
                pass
            raise SendError('send_rate_limited', delay)
        if isinstance(http_status, int) and http_status >= 500:
            raise SendError('send_server_error', 2)
        raise SendError(f'send_rejected_{code}')
