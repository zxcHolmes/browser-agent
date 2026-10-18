"""Runnable examples for the shared Browser Agent high-level actions.

  python3 cli/demo.py local
  python3 cli/demo.py hn
  python3 cli/demo.py upload --url URL --file /absolute/video.mp4

The default local demo uploads only to its temporary localhost server. The HN
example never submits. The upload mode selects the explicit files on the explicit
website, optionally waits for a site-specific condition, and leaves the page open.
Only Python's standard library is required. See --help for all options.
"""

import argparse
import hashlib
import json
from pathlib import Path
import sys
import tempfile
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.error import URLError


try:
    from .common.browser_client import APIError, BASE, BrowserClient, TransportError
except ImportError:  # run as a script: cli/ is on sys.path
    from common.browser_client import APIError, BASE, BrowserClient, TransportError


LOCAL_HTML = """<!doctype html><meta charset="utf-8"><title>Browser Agent local demo</title>
<h1>Local article draft</h1>
<label>Title <input id="title"></label><br>
<label>Article <div id="article" contenteditable="true" role="textbox" aria-label="Article"></div></label>
<label><input id="agree" type="checkbox">Ready for review</label>
<select id="category"><option value="article">Article</option><option value="video">Video</option></select>
<input id="file" type="file" hidden>
<button id="save">Save draft</button><output id="status">idle</output>
<script>
window.draft={title:'',article:'',upload:null};
document.querySelector('#title').addEventListener('input',e=>draft.title=e.target.value);
document.querySelector('#article').addEventListener('input',e=>draft.article=e.target.innerText);
document.querySelector('#file').addEventListener('change',async e=>{
 const file=e.target.files[0]; if(!file)return;
 document.querySelector('#status').textContent='uploading';
 const response=await fetch('/upload',{method:'POST',body:file});
 if(!response.ok)throw new Error('Upload failed');
 draft.upload={name:file.name,...await response.json()};
 document.querySelector('#status').textContent='uploaded';
});
document.querySelector('#save').onclick=()=>{
 localStorage.setItem('demoDraft',JSON.stringify(draft));
 document.querySelector('#status').textContent='saved';
};
</script>"""


class LocalFixture(BaseHTTPRequestHandler):
    def log_message(self, *_):
        pass

    def do_GET(self):
        payload = LOCAL_HTML.encode()
        self.send_response(200)
        self.send_header('Content-Type', 'text/html; charset=utf-8')
        self.send_header('Content-Length', str(len(payload)))
        self.end_headers()
        self.wfile.write(payload)

    def do_POST(self):
        data = self.rfile.read(int(self.headers['Content-Length']))
        result = json.dumps({'bytes': len(data), 'sha256': hashlib.sha256(data).hexdigest()}).encode()
        self.send_response(200)
        self.send_header('Content-Type', 'application/json')
        self.send_header('Content-Length', str(len(result)))
        self.end_headers()
        self.wfile.write(result)


def finish_demo_tab(client, tab_id, keep_tab):
    if keep_tab:
        client.release_tab(tab_id)
        print(f'Left demo page open and released tab {tab_id}.')
    else:
        client.close_tab(tab_id)


def flow_local(client, output, keep_tab=False):
    """Fill, upload, save, reload, and verify without touching a third-party account."""
    server = ThreadingHTTPServer(('127.0.0.1', 0), LocalFixture)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    url = f'http://127.0.0.1:{server.server_port}/'
    tab_id = None
    try:
        tab_id = client.create_tab(auto_close=False, meta={'demo': 'local-draft'})['tabId']
        client.navigate(tab_id, url)
        client.wait_for(tab_id, target='#title', state='visible')
        snapshot = client.action(tab_id, 'snapshot')
        title = next(e for e in snapshot['elements'] if e['role'] == 'textbox' and e['name'] == 'Title')
        expected_title = '浏览器自动化示例 🎬'
        client.action(tab_id, 'fill', target={'ref': title['ref']}, value=expected_title)
        client.action(tab_id, 'fill', target='#article', value='第一段：本地草稿。\n第二段：上传并核验。')
        client.action(tab_id, 'set_checked', target='#agree', checked=True)
        client.action(tab_id, 'select_option', target='#category', values=['video'])
        with tempfile.TemporaryDirectory(prefix='browser-agent-demo-') as directory:
            file = Path(directory) / '上传示例.txt'
            payload = ('Local upload integrity example 中文\n' * 4096).encode()
            file.write_bytes(payload)
            selected = client.action(tab_id, 'upload_files', target='#file', files=[str(file)])
            if selected['stage'] != 'selected':
                raise RuntimeError('File selection was not acknowledged')
            client.wait_for(tab_id, target='#status', state='text', value='uploaded')
            upload = client.evaluate(tab_id, 'draft.upload')
            if upload['sha256'] != hashlib.sha256(payload).hexdigest() or upload['bytes'] != len(payload):
                raise RuntimeError('Received file differs from the selected file')
        client.action(tab_id, 'click', target={'role': 'button', 'name': 'Save draft'})
        client.wait_for(tab_id, target='#status', state='text', value='saved')
        shot = client.screenshot(tab_id, Path(output) / 'local-draft.png')
        client.navigate(tab_id, url)
        client.wait_for(tab_id, target='#title', state='visible')
        saved = client.evaluate(tab_id, "JSON.parse(localStorage.getItem('demoDraft'))")
        if saved['title'] != expected_title or saved['upload']['sha256'] != upload['sha256']:
            raise RuntimeError('Saved draft did not survive navigation')
        print(json.dumps({'draftVerifiedAfterReload': True, 'upload': upload, 'screenshot': str(shot)},
                         ensure_ascii=False, indent=2))
    finally:
        try:
            if tab_id is not None:
                finish_demo_tab(client, tab_id, keep_tab)
        finally:
            server.shutdown()
            server.server_close()
    if keep_tab:
        print('The temporary local server has stopped; the currently rendered page remains visible.')


def flow_scrape_hn(client):
    tab_id = client.create_tab(meta={'demo': 'scrape-hn'})['tabId']
    try:
        client.navigate(tab_id, 'https://news.ycombinator.com/')
        client.wait_for(tab_id, state='text', value='Hacker News', timeout=15)
        stories = client.evaluate(tab_id, """[...document.querySelectorAll('tr.athing')].map(row=>({
            title:row.querySelector('.titleline > a')?.innerText,
            url:row.querySelector('.titleline > a')?.href
        }))""")
        if not stories:
            raise RuntimeError('No Hacker News story rows found; inspect the current page before adapting extraction')
        print(json.dumps(stories[:5], ensure_ascii=False, indent=2))
    finally:
        client.close_tab(tab_id)


def flow_fill_hn_submit(client, output, keep_tab=False):
    tab_id = client.create_tab(auto_close=False, meta={'demo': 'fill-hn'})['tabId']
    try:
        client.navigate(tab_id, 'https://news.ycombinator.com/submit')
        snapshot = client.action(tab_id, 'snapshot')
        if not any(e.get('tag') == 'input' and e.get('type') == 'text' for e in snapshot['elements']):
            print('Hacker News submit form is unavailable; skipping (login may be required).')
            return
        # Check the actual submit fields rather than editing a login page.
        if not client.evaluate(tab_id, '!!document.querySelector(\'input[name="title"]\')'):
            print('Hacker News requires login; form filling skipped. Nothing was submitted.')
            return
        for target, value in [
            ('input[name="title"]', 'Browser Agent Proxy — semantic browser automation'),
            ('input[name="url"]', 'https://example.com/'),
            ('textarea[name="text"]', 'Demonstration only. This form will not be submitted.'),
        ]:
            client.wait_for(tab_id, target=target, state='visible')
            client.action(tab_id, 'fill', target=target, value=value)
            client.wait_for(tab_id, target=target, state='value', value=value)
        print('Filled and verified the HN form. Nothing was submitted.')
        print('Screenshot:', client.screenshot(tab_id, Path(output) / 'hn-form.png'))
    finally:
        finish_demo_tab(client, tab_id, keep_tab)


def flow_upload(client, args):
    """Explicit remote file selection. Preserve the page on success and failure."""
    files = [Path(path).expanduser().resolve() for path in args.file]
    for path in files:
        if not path.is_file():
            raise ValueError(f'Not a regular local file: {path}')
    tab_id = client.create_tab(auto_close=False, meta={'demo': 'explicit-upload'})['tabId']
    print(f'Upload tab: {tab_id}; auto-close disabled.', flush=True)
    try:
        client.navigate(tab_id, args.url)
        client.wait_for(tab_id, target=args.selector, state='attached')
        result = client.action(tab_id, 'upload_files', target=args.selector, files=[str(path) for path in files])
        print(json.dumps(result, ensure_ascii=False, indent=2), flush=True)
        if args.complete_selector:
            client.wait_for(tab_id, target=args.complete_selector, state='text',
                            value=args.complete_text, timeout=args.wait_seconds)
            print('The requested site condition matched. Verify processing/draft/publication separately.')
        else:
            print('Files selected. Transfer and processing completion have not been verified.')
    finally:
        if args.keep_tab:
            print(f'Kept tab {tab_id} managed with auto-close disabled; close or release it when finished.')
        else:
            client.release_tab(tab_id)
            print(f'Released tab {tab_id} without closing the upload page; continue manually if needed.')


def main():
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument('mode', nargs='?', choices=['local', 'hn', 'upload'], default='local')
    parser.add_argument('--base', default=BASE, help='Go proxy URL')
    parser.add_argument('--output', default='/tmp/browser-agent-demo', help='Screenshot directory')
    parser.add_argument('--keep-tab', action='store_true', help='Keep pages open; upload mode also keeps management')
    parser.add_argument('--url', help='Explicit upload destination, required for upload mode')
    parser.add_argument('--file', action='append', help='Local file to select; repeat for multiple files')
    parser.add_argument('--selector', default='input[type=file]', help='Unique file input selector (hidden allowed)')
    parser.add_argument('--complete-selector', help='Optional unique site status element to wait for')
    parser.add_argument('--complete-text', help='Expected status text; required with --complete-selector')
    parser.add_argument('--wait-seconds', type=float, default=240, help='Site condition timeout, >0 and <=300')
    args = parser.parse_args()
    if args.mode == 'upload' and (not args.url or not args.file):
        parser.error('upload mode requires --url and at least one --file')
    if (args.complete_selector is None) != (args.complete_text is None):
        parser.error('--complete-selector and --complete-text must be supplied together')
    if not 0 < args.wait_seconds <= 300:
        parser.error('--wait-seconds must be >0 and <=300')
    client = BrowserClient(args.base)
    try:
        caps = client.capabilities()
        required = {'snapshot', 'fill', 'wait_for', 'upload_files', 'click'}
        if not required.issubset(caps.get('actions', [])):
            raise RuntimeError('Loaded extension lacks the high-level actions; reload Browser Agent')
        if args.mode == 'local':
            flow_local(client, args.output, args.keep_tab)
        elif args.mode == 'hn':
            flow_scrape_hn(client)
            flow_fill_hn_submit(client, args.output, args.keep_tab)
        else:
            flow_upload(client, args)
        return 0
    except (APIError, TransportError, URLError, TimeoutError, ValueError, RuntimeError) as error:
        print(f'Error: {error}', file=sys.stderr)
        print('No mutation was automatically retried. Inspect any preserved page before retrying.', file=sys.stderr)
        return 1


if __name__ == '__main__':
    raise SystemExit(main())
