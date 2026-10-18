"""Exercise the real Go -> extension -> Chrome bridge against a local fixture.

Run from the repository root: python3 cli/browser_smoke.py.
No account access or third-party uploads; closes only its own test tab.
"""

import argparse
import hashlib
import json
from pathlib import Path
import tempfile
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.error import HTTPError
from urllib.request import Request, urlopen


PAGE = """<!doctype html><meta charset="utf-8"><title>Browser Agent smoke test</title>
<h1>Local publishing fixture</h1>
<label>Title <input id="title"></label>
<label><input id="agree" type="checkbox">Agree</label>
<select id="category"><option value="a">Article</option><option value="v">Video</option></select>
<button class="duplicate">Duplicate</button><button class="duplicate">Duplicate</button>
<button id="confirm" onclick="document.body.dataset.confirmed=String(confirm('Save this draft?'))">Confirm draft</button>
<div id="editor" contenteditable="true" aria-label="Article"></div>
<input id="file" type="file" hidden>
<button id="choose" onclick="document.querySelector('#file').click()">Choose file</button>
<button id="save">Save local draft</button><output id="status">idle</output>
<div id="shadow"></div><iframe id="frame" src="/frame"></iframe>
<iframe id="cross" src="http://localhost:PORT/frame"></iframe>
<a id="popup" href="/frame" target="_blank">Open editor window</a>
<script>
window.state={title:'',article:'',upload:null};
titleInput=document.querySelector('#title');
titleInput.addEventListener('input',()=>state.title=titleInput.value);
document.querySelector('#editor').addEventListener('input',e=>state.article=e.target.innerText);
document.querySelector('#shadow').attachShadow({mode:'open'}).innerHTML='<label>Tag <input id="tag"></label>';
document.querySelector('#file').addEventListener('change',async e=>{
  const f=e.target.files[0]; if(!f)return;
  document.querySelector('#status').textContent='uploading';
  const r=await fetch('/upload',{method:'POST',body:f});
  state.upload={name:f.name,size:f.size,...await r.json()};
  document.querySelector('#status').textContent='uploaded';
});
document.querySelector('#save').onclick=()=>{
  localStorage.setItem('draft',JSON.stringify(state));
  document.querySelector('#status').textContent='saved';
};
setTimeout(()=>{const b=document.createElement('button');b.id='late';b.textContent='Ready';document.body.append(b)},1800);
</script>"""


class Fixture(BaseHTTPRequestHandler):
    def log_message(self, *_):
        pass

    def do_GET(self):
        body = ("<title>Frame</title><input id='inside' aria-label='Frame field'><button id='frame-button' onclick=\"document.body.dataset.clicked='yes'\">Frame action</button>"
                if self.path == '/frame' else PAGE.replace('PORT', str(self.server.server_port)))
        data = body.encode()
        self.send_response(200)
        self.send_header('Content-Type', 'text/html; charset=utf-8')
        self.send_header('Content-Length', str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def do_POST(self):
        data = self.rfile.read(int(self.headers['Content-Length']))
        result = json.dumps({'received': len(data), 'sha256': hashlib.sha256(data).hexdigest()}).encode()
        self.send_response(200)
        self.send_header('Content-Type', 'application/json')
        self.end_headers()
        self.wfile.write(result)


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--base', default='http://127.0.0.1:12345')
    parser.add_argument('--output', default='/tmp/browser-agent-smoke')
    parser.add_argument('--advanced', action='store_true', help='Verify the shared high-level browser tools')
    args = parser.parse_args()
    out = Path(args.output)
    out.mkdir(parents=True, exist_ok=True)
    checks = []
    tab_id = None
    popup_opened = False
    child_tabs = []

    def request(path, payload=None, method=None):
        data = json.dumps(payload).encode() if payload is not None else None
        req = Request(args.base + path, data=data, method=method,
                      headers={'Content-Type': 'application/json'})
        try:
            with urlopen(req, timeout=40) as r:
                return json.load(r)
        except HTTPError as e:
            raise RuntimeError(e.read().decode()) from e

    def cdp(method, params=None):
        return request('/api/cdp', {'tabId': tab_id, 'method': method, 'params': params or {}})['data']['result']

    def evaluate(expression):
        return request(f'/api/tabs/{tab_id}/eval', {'expression': expression, 'awaitPromise': True})['data'].get('value')

    def action(name, **kwargs):
        return request(f'/api/tabs/{tab_id}/action', {'action': name, 'args': kwargs})['data']

    def expected_failure(fn, text):
        try:
            fn()
        except RuntimeError as e:
            return require(text in str(e), str(e))
        raise AssertionError('expected action to fail: ' + text)

    def check(name, fn):
        try:
            detail = fn()
            checks.append({'name': name, 'passed': True, 'detail': detail})
            print('PASS', name, json.dumps(detail, ensure_ascii=False), flush=True)
        except Exception as e:
            checks.append({'name': name, 'passed': False, 'detail': str(e)})
            print('FAIL', name, str(e), flush=True)

    def require(condition, detail):
        if not condition:
            raise AssertionError(str(detail))
        return detail

    def wait(expression, seconds=8):
        deadline = time.monotonic() + seconds
        while time.monotonic() < deadline:
            result = evaluate(expression)
            if result:
                return result
            time.sleep(.2)
        raise TimeoutError(expression)

    fixture = ThreadingHTTPServer(('127.0.0.1', 0), Fixture)
    threading.Thread(target=fixture.serve_forever, daemon=True).start()
    url = f'http://127.0.0.1:{fixture.server_port}/'
    try:
        tab = request('/api/tabs', {'url': 'about:blank', 'active': False, 'autoClose': False,
                                  'meta': {'test': 'browser-smoke'}})['data']['tab']
        tab_id = tab['tabId']
        check('create persistent managed tab', lambda: require(tab.get('autoClose') is False, tab))
        check('navigate and load event', lambda: require(
            (nav := request(f'/api/tabs/{tab_id}/navigate', {'url': url})['data']).get('loadEventFired'), nav))
        check('DOM read', lambda: require(evaluate('document.title') == 'Browser Agent smoke test', 'title matches'))
        text = '自动化测试标题 🎬'

        def type_title():
            evaluate("document.querySelector('#title').focus()")
            cdp('Input.insertText', {'text': text})
            return require(evaluate('state.title') == text, evaluate('state'))
        check('Chinese input and application input event', type_title)

        def rich_text():
            evaluate("document.querySelector('#editor').focus()")
            cdp('Input.insertText', {'text': '第一段：测试文章。\n第二段：保存草稿。'})
            value = evaluate('state.article')
            return require('第一段' in value and '第二段' in value, value)
        check('multiline contenteditable and input event', rich_text)
        check('Shadow DOM input', lambda: require(evaluate("(()=>{const e=document.querySelector('#shadow').shadowRoot.querySelector('#tag'); e.value='automation';return e.value})()") == 'automation', 'open shadow root accessible'))
        check('same-origin iframe', lambda: require(evaluate("(()=>{const e=document.querySelector('#frame').contentDocument.querySelector('#inside');e.value='frame test';return e.value})()") == 'frame test', 'frame input accessible'))

        def cross_frame():
            try:
                evaluate("document.querySelector('#cross').contentWindow.document.body.innerText")
            except RuntimeError as e:
                return require('SecurityError' in str(e) or 'cross-origin' in str(e) or 'Blocked' in str(e), 'top-frame JS blocked across origins as expected')
            raise AssertionError('expected cross-origin restriction')
        check('cross-origin frame boundary', cross_frame)
        check('delayed SPA element condition wait', lambda: wait("!!document.querySelector('#late')"))

        if args.advanced:
            check('advanced capabilities and 30 minute default', lambda: require(
                (caps := request('/api/capabilities')['data'])['defaultIdleTimeoutMinutes'] == 30
                and 'upload_files' in caps['actions'], caps))
            def snapshot_check():
                snap = action('snapshot')
                return require(any(e['name'] == 'Title' for e in snap['elements']), {'elements': len(snap['elements'])})
            check('advanced snapshot', snapshot_check)
            title_ref = next(e['ref'] for e in action('snapshot')['elements'] if e['name'] == 'Title')
            check('advanced fill by ref replaces existing text', lambda: require(
                action('fill', target={'ref': title_ref}, value=text)['value'] == text
                and evaluate('state.title') == text, 'replaced and application state verified'))
            check('advanced keyboard select all', lambda: action('press', target='#title', key='Meta+a'))
            check('advanced fill after keyboard selection', lambda: action('fill', target='#title', value=text))
            check('advanced rich text replacement', lambda: require(
                '新的正文' in action('fill', target='#editor', value='新的正文\n第二段')['value'], 'contenteditable replaced'))
            check('advanced semantic checkbox', lambda: require(
                action('set_checked', target={'role': 'checkbox', 'name': 'Agree'}, checked=True)['checked'], 'checked'))
            check('advanced select option', lambda: require(
                action('select_option', target='#category', values=['v'])['values'] == ['v'], 'selected video'))
            check('advanced Shadow DOM fill', lambda: require(
                action('fill', target={'role': 'textbox', 'name': 'Tag'}, value='shadow text')['value'] == 'shadow text', 'shadow input verified'))
            check('advanced same-origin frame fill', lambda: require(
                action('fill', target='#inside', frame_selector='#frame', value='iframe input')['value'] == 'iframe input', 'frame value verified'))

            def cross_origin_action():
                frames = action('list_frames')
                contexts = [c for c in frames['contexts'] if c.get('isDefault') and c.get('origin', '').startswith('http://localhost:')]
                require(bool(contexts), frames)
                context = contexts[0]
                value = action('fill', target='#inside', value='cross origin input', context_id=context['id'], session_id=context['sessionId'])
                require(value['value'] == 'cross origin input', value)
                return value
            check('advanced cross-origin frame routing', cross_origin_action)
            check('advanced missing local file rejected before dispatch', lambda: expected_failure(
                lambda: action('upload_files', target='#file', files=['/nonexistent/browser-agent-test-file.mp4']), 'INVALID_UPLOAD_FILES'))
            check('advanced ambiguity rejection', lambda: expected_failure(
                lambda: action('click', target={'role': 'button', 'name': 'Duplicate'}), 'AMBIGUOUS_TARGET'))
            check('advanced wait timeout is an error', lambda: expected_failure(
                lambda: action('wait_for', target='#does-not-exist', timeout_seconds=.05), 'WAIT_TIMEOUT'))
            check('advanced per-tab lifetime policy', lambda: require(
                action('set_tab_policy', auto_close=False, idle_timeout_minutes=60)['idleTimeoutMinutes'] == 60, '60 minutes, auto-close disabled'))

            def dialog():
                try:
                    action('click', target='#confirm')
                except RuntimeError as e:
                    if 'JAVASCRIPT_DIALOG_OPEN' not in str(e):
                        raise
                result = action('handle_dialog', accept=False)
                return require(result['handled'] and evaluate('document.body.dataset.confirmed') == 'false', 'dialog dismissed and page continued')
            check('advanced JavaScript confirm without queue deadlock', dialog)

        with tempfile.TemporaryDirectory(prefix='browser-agent-upload-') as tmp:
            file = Path(tmp) / '上传测试.txt'
            payload = ('Upload integrity test 中文\n' * 4096).encode()
            file.write_bytes(payload)

            def upload():
                if args.advanced:
                    selected = action('upload_files', target='#file', files=[str(file)])
                    require(selected['stage'] == 'selected', selected)
                    action('wait_for', target='#status', state='text', value='uploaded')
                else:
                    root = cdp('DOM.getDocument')['root']['nodeId']
                    node = cdp('DOM.querySelector', {'nodeId': root, 'selector': '#file'})['nodeId']
                    cdp('DOM.setFileInputFiles', {'nodeId': node, 'files': [str(file)]})
                result = wait('state.upload')
                return require(result['sha256'] == hashlib.sha256(payload).hexdigest()
                               and result['received'] == len(payload) and result['name'] == file.name, result)
            check('hidden file input: actual HTTP upload and SHA256', upload)

        def chooser():
            cdp('Page.setInterceptFileChooserDialog', {'enabled': True})
            try:
                evaluate("document.querySelector('#choose').click()")
                data = request(f'/api/events?tabId={tab_id}&method=Page.fileChooserOpened')['data']
                return require(data['count'] > 0, {'count': data['count']})
            finally:
                cdp('Page.setInterceptFileChooserDialog', {'enabled': False})
        check('file chooser interception and event forwarding', chooser)

        def save_reload():
            if args.advanced:
                action('click', target={'role': 'button', 'name': 'Save local draft'})
                action('wait_for', target='#status', state='text', value='saved')
            else:
                evaluate("document.querySelector('#save').click()")
            request(f'/api/tabs/{tab_id}/navigate', {'url': url})
            draft = evaluate("JSON.parse(localStorage.getItem('draft'))")
            return require(draft['title'] == text and draft['upload']['received'] > 0, draft)
        check('save draft and verify after reload', save_reload)

        def popup():
            nonlocal popup_opened
            if args.advanced:
                action('click', target='#popup')
                deadline = time.monotonic() + 3
                while time.monotonic() < deadline:
                    managed = request('/api/tabs')['data']['tabs']
                    children = [t for t in managed if t.get('meta', {}).get('parentTabId') == tab_id]
                    if children:
                        child_tabs.extend(t['tabId'] for t in children)
                        return {'popupExists': True, 'popupManaged': True, 'parentTabId': tab_id}
                    time.sleep(.1)
                raise AssertionError('trusted link click did not produce a managed child tab')
            opened = cdp('Runtime.evaluate', {
                'expression': "window.testPopup=window.open('/frame','_blank'); !!window.testPopup && !window.testPopup.closed",
                'returnByValue': True, 'userGesture': True})
            popup_opened = bool(opened.get('result', {}).get('value'))
            wait("window.testPopup && !window.testPopup.closed && window.testPopup.document.title === 'Frame'")
            managed = request('/api/tabs')['data']['tabs']
            if args.advanced:
                deadline = time.monotonic() + 3
                while not any(t.get('meta', {}).get('parentTabId') == tab_id for t in managed) and time.monotonic() < deadline:
                    time.sleep(.1)
                    managed = request('/api/tabs')['data']['tabs']
            child_tabs.extend(t['tabId'] for t in managed if t.get('meta', {}).get('parentTabId') == tab_id)
            if args.advanced:
                require(bool(child_tabs), {'error': 'child window not managed', 'tabs': managed})
            return require(popup_opened, {'popupExists': popup_opened,
                           'popupManaged': any(t['tabId'] != tab_id and t['url'].startswith(url) for t in managed)})
        check('popup creation and management observation', popup)

        def restricted_targets():
            try:
                cdp('Target.getTargets')
            except RuntimeError as e:
                return require('Not allowed' in str(e), 'Target.getTargets is restricted by chrome.debugger')
            return 'Target.getTargets available in this Chrome version'
        check('CDP target enumeration capability probe', restricted_targets)

        def screenshot():
            with urlopen(args.base + f'/api/tabs/{tab_id}/screenshot', timeout=30) as r:
                data = r.read()
            require(data.startswith(b'\x89PNG'), 'PNG signature')
            (out / 'local-fixture.png').write_bytes(data)
            return {'bytes': len(data), 'path': str(out / 'local-fixture.png')}
        check('screenshot binary endpoint', screenshot)
    finally:
        for child in child_tabs:
            check('close managed child tab', lambda child=child: request(f'/api/tabs/{child}', method='DELETE'))
        if popup_opened and tab_id:
            try:
                evaluate('window.testPopup.close(); true')
            except Exception as e:
                print('Popup cleanup:', e, flush=True)
        if tab_id:
            check('close managed tab', lambda: request(f'/api/tabs/{tab_id}', method='DELETE'))
        fixture.shutdown()
        report = {'base': args.base, 'checks': checks,
                  'passed': sum(c['passed'] for c in checks), 'total': len(checks)}
        report_path = out / ('report-advanced.json' if args.advanced else 'report.json')
        report_path.write_text(json.dumps(report, ensure_ascii=False, indent=2))
        print('Report:', report_path, flush=True)
    return 0 if all(c['passed'] for c in checks) else 1


if __name__ == '__main__':
    raise SystemExit(main())
