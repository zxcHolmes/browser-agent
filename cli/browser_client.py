"""Dependency-free client for Browser Agent Proxy's managed browser API."""

import json
from http.client import HTTPException
import socket
from pathlib import Path
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen

BASE = "http://127.0.0.1:12345"


ASSETS = Path(__file__).parent / 'assets'


def load_probe(name):
    """A platform probe (an IIFE expression in assets/) with the shared DOM
    helpers in scope. Both are wrapped in one function so repeated evaluations
    never redeclare top-level bindings in the page."""
    helpers = (ASSETS / 'dom_helpers.js').read_text(encoding='utf-8')
    probe = (ASSETS / name).read_text(encoding='utf-8').strip().rstrip(';')
    return f'(() => {{\n{helpers}\nreturn {probe};\n}})()'


class APIError(RuntimeError):
    def __init__(self, status, code, message):
        super().__init__(f"HTTP {status} {code}: {message}")
        self.status, self.code = status, code


class TransportError(RuntimeError):
    """No trustworthy API reply; a mutation may already have taken effect."""


class BrowserClient:
    def __init__(self, base=BASE):
        self.base = base.rstrip('/')

    def request(self, path, body=None, *, method=None, timeout=45):
        req = Request(self.base + path, method=method,
                      data=json.dumps(body).encode() if body is not None else None,
                      headers={'Content-Type': 'application/json'})
        try:
            with urlopen(req, timeout=timeout) as response:
                envelope = json.load(response)
        except HTTPError as error:
            raw = error.read().decode(errors='replace')
            try:
                payload = json.loads(raw)
            except ValueError:
                payload = {'error': raw[:600]}
            if not isinstance(payload, dict):
                payload = {'error': raw[:600]}
            raise APIError(error.code, payload.get('code', 'HTTP_ERROR'), payload.get('error', raw)) from error
        except (URLError, OSError, socket.timeout, HTTPException) as error:
            raise TransportError(f'Cannot obtain an API response from {self.base}: {error}') from error
        except (ValueError, UnicodeError) as error:
            raise TransportError('Proxy returned invalid JSON; verify --api-url and server version') from error
        if not isinstance(envelope, dict):
            raise TransportError('Proxy returned a non-object response')
        if not envelope.get('success'):
            raise APIError(200, envelope.get('code', 'API_ERROR'), envelope.get('error', 'Unknown failure'))
        if not isinstance(envelope.get('data'), dict):
            raise TransportError('Proxy response is missing object data')
        return envelope['data']

    def capabilities(self):
        return self.request('/api/capabilities', timeout=15)

    def tabs(self):
        return self.request('/api/tabs')['tabs']

    def tab(self, tab_id):
        return self.request(f'/api/tabs/{tab_id}')['tab']

    def cdp(self, tab_id, method, params=None, *, timeout=30, session_id=''):
        return self.request('/api/cdp', {'tabId':tab_id,'method':method,'params':params or {},
                                       'timeout':timeout,'sessionId':session_id},timeout=timeout+5)['result']

    def create_tab(self, url='about:blank', *, auto_close=True, meta=None):
        return self.request('/api/tabs', {'url': url, 'active': False,
                                         'autoClose': auto_close, 'meta': meta or {}})['tab']

    def close_tab(self, tab_id):
        return self.request(f'/api/tabs/{tab_id}', method='DELETE')

    def release_tab(self, tab_id):
        return self.request(f'/api/tabs/{tab_id}/release', {}, method='POST')

    def navigate(self, tab_id, url, timeout=30):
        # Legacy /navigate can spend up to timeout on dispatch, then another
        # timeout on its load-event wait. SPA readiness needs wait_for below.
        result = self.request(f'/api/tabs/{tab_id}/navigate',
                              {'url': url, 'waitForLoad': True, 'timeoutSeconds': timeout},
                              timeout=2 * timeout + 5)
        if result.get('errorText'):
            raise APIError(502, 'NAVIGATION_FAILED', result['errorText'])
        return result

    def action(self, tab_id, name, *, request_timeout=40, **args):
        return self.request(f'/api/tabs/{tab_id}/action',
                            {'action': name, 'args': args, 'timeoutSeconds': request_timeout},
                            timeout=request_timeout + 5)

    def wait_for(self, tab_id, *, timeout=30, **args):
        if not 0 < timeout <= 300:
            raise ValueError('wait timeout must be > 0 and <= 300 seconds')
        # The caller, Go request, and extension wait have separate deadlines.
        return self.action(tab_id, 'wait_for', request_timeout=max(40, int(timeout) + 10),
                           timeout_seconds=timeout, **args)

    def evaluate(self, tab_id, expression):
        return self.request(f'/api/tabs/{tab_id}/eval',
                            {'expression': expression, 'awaitPromise': True}).get('value')

    def screenshot(self, tab_id, path):
        with urlopen(self.base + f'/api/tabs/{tab_id}/screenshot', timeout=35) as response:
            data = response.read()
        if not data.startswith(b'\x89PNG'):
            raise RuntimeError('Screenshot endpoint did not return a PNG')
        path = Path(path)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(data)
        return path


