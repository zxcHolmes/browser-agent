"""Upload, save drafts, or publish videos through Douyin Creator Center in Chrome."""
import argparse
import base64
import contextlib
import json
import os
from pathlib import Path
import re
import shlex
import sys
import time
import uuid
from urllib.parse import urlparse

from ..common.automation import AutomationError, FileLock, TaskState, claim_key
from ..common.browser_client import APIError, BASE, BrowserClient, TransportError, load_probe
from ..common.media import fingerprint, read_text
from ..common.page_workflow import PageWorkflow, MemoryState, same_text
from ..common.publications import observation_key,listing_result,select_publications,scroll_list
from ..common.scheduling import schedule_time, display_time

ROOT = 'https://creator.douyin.com/creator-micro'
UPLOAD_URL = ROOT + '/content/upload'
HOME_URL = ROOT + '/home'
CONTENT_URL = ROOT + '/content/manage'
PROBE = load_probe(Path(__file__).with_name('probe.js'))
DEFAULT_STATE_DIR = Path(os.environ.get('XDG_STATE_HOME', str(Path.home() / '.local/state'))) / 'browser-agent/douyin'
VISIBILITY = {'public': '0', 'private': '1', 'friends': '2'}
VIDEO_TYPES = {'.mp4', '.mov', '.m4v', '.avi', '.wmv', '.webm', '.ts', '.flv', '.mkv', '.mpg', '.mpeg4', '.m4'}


def positive(value):
    try:
        n = float(value)
    except ValueError:
        raise argparse.ArgumentTypeError('Enter a finite number of seconds greater than zero')
    if not 0 < n < float('inf'):
        raise argparse.ArgumentTypeError('Enter a finite number of seconds greater than zero')
    return n


def validate_state(data):
    c = data['config']
    valid = (c.get('mode') in {'upload', 'draft', 'post'} and isinstance(c.get('title'), str)
             and isinstance(c.get('description'), str) and isinstance(c.get('video'), dict)
             and isinstance(c.get('state_dir'), str) and c.get('visibility') in VISIBILITY)
    if not valid or not isinstance(c['video'].get('path'), str) or not re.fullmatch('[0-9a-f]{64}', str(c['video'].get('sha256'))):
        raise AutomationError('INVALID_STATE', 'Invalid video, text, or configuration in state file', exit_code=2)
    video = c['video']
    if (not isinstance(video.get('size'), int) or video['size'] <= 0
            or not isinstance(video.get('mtime_ns'), int) or video['mtime_ns'] < 0
            or not all(isinstance(c.get(k), bool) for k in ['reuse_draft', 'allow_duplicate'])
            or c.get('account') is not None and not isinstance(c['account'], str)):
        raise AutomationError('INVALID_STATE', 'Invalid video metadata or options in state file', exit_code=2)
    for key in ['step_timeout', 'poll_interval', 'upload_timeout']:
        if not isinstance(c.get(key), (float, int)) or not 0 < c[key] < float('inf'):
            raise AutomationError('INVALID_STATE', f'Invalid timeout: {key}', exit_code=2)
    if not c['title'].strip() or len(c['title']) > 30 or len(c['description']) > 1000:
        raise AutomationError('INVALID_STATE', 'Invalid title or description length in state file', exit_code=2)


def load_state(path):
    return TaskState.load(path, platform='douyin', validate=validate_state)


def prepare(args):
    title = read_text(args.title, args.title_file, Path(args.video).stem).strip()
    description = read_text(args.description, args.description_file).replace('\r\n', '\n').rstrip('\n')
    if not title or len(title) > 30 or '\n' in title or '\x00' in title:
        raise AutomationError('INVALID_TITLE', 'Title must be 1–30 characters on one line; use --title or --title-file', exit_code=2)
    if len(description) > 1000 or '\x00' in description:
        raise AutomationError('INVALID_DESCRIPTION', 'Description must contain at most 1000 characters and no NUL', exit_code=2)
    if Path(args.video).suffix.lower() not in VIDEO_TYPES:
        raise AutomationError('INVALID_VIDEO', 'Unsupported video extension; MP4 is recommended', exit_code=2)
    video = fingerprint(args.video, quiet=True, code='INVALID_VIDEO')
    if video['size'] > 16 * 1024 ** 3:
        raise AutomationError('INVALID_VIDEO', 'Video exceeds the 16 GiB web upload limit', exit_code=2)
    root = args.state_dir.expanduser().resolve()
    job_id = uuid.uuid4().hex
    path = args.state_file.expanduser().resolve() if args.state_file else root / job_id / 'state.json'
    if path.exists():
        raise AutomationError('STATE_EXISTS', 'State file exists; use resume or choose a new path', exit_code=2)
    scheduled = schedule_time(getattr(args, 'schedule_at', None), min_lead=7200, max_days=14)
    if scheduled and getattr(args, 'draft', False):
        raise AutomationError('INVALID_SCHEDULE', '--draft cannot be combined with --schedule-at', exit_code=2)
    return TaskState(path, {'schema_version': 1, 'platform': 'douyin', 'job_id': job_id,
                           'phase': 'prepared', 'status': 'prepared', 'created_at': time.time(), 'api_url': args.api_url or BASE,
                           'config': {'mode': 'draft' if getattr(args, 'draft', False) else args.command,
                                      'video': video, 'title': title, 'description': description,
                                      'visibility': args.visibility, 'account': args.account,
                                      'schedule_at': scheduled.isoformat() if scheduled else None,
                                      'reuse_draft': args.reuse_draft, 'allow_duplicate': args.allow_duplicate,
                                      'upload_timeout': args.upload_timeout, 'step_timeout': args.step_timeout,
                                      'poll_interval': args.poll_interval, 'state_dir': str(root)}})


class DouyinWorkflow(PageWorkflow):
    def __init__(self, client, state, **kwargs):
        super().__init__(client, state, PROBE, **kwargs)

    def guard(self, p):
        if p.get('challenge'):
            raise AutomationError('VERIFICATION_REQUIRED', 'Complete the security challenge in the retained tab, then resume', exit_code=3)
        if p.get('loginRequired'):
            raise AutomationError('LOGIN_REQUIRED', 'Sign in to Douyin Creator Center in the retained tab, then resume', exit_code=3)
        if p.get('host') and p['host'] != 'creator.douyin.com':
            raise AutomationError('UNEXPECTED_ORIGIN', f"Page navigated to an unexpected origin: {p['host']}", exit_code=3)
        if p.get('rejected'):
            raise AutomationError('SITE_REJECTED', 'Douyin returned an error: ' + '; '.join(p.get('alerts', [])), exit_code=6,
                                  details={'alerts': p.get('alerts')})
        if p.get('dialog'):
            raise AutomationError('DIALOG_REQUIRES_ATTENTION', 'A page dialog requires attention: ' + p['dialog'], exit_code=4)

    def open(self, url, ready):
        self.navigate(url)
        return self.poll(ready, self.config['step_timeout'], 'PAGE_NOT_READY_TIMEOUT', 'Page did not load as expected; check the connection, login, or site changes')

    def check_account(self):
        self.phase('account', 'Check the signed-in account')
        p = self.open(HOME_URL, lambda p: p.get('account'))
        expected = self.config.get('account') or self.state.data.get('account')
        if expected and p['account'] != expected:
            raise AutomationError('ACCOUNT_MISMATCH', f"Current Douyin ID: {p['account']}; expected: {expected}", exit_code=3)
        self.state.save(account=p['account'])

    def owned_draft(self, p):
        video = self.config['video']
        matches = [d for d in p.get('drafts', []) if d.get('file', {}).get('name') == Path(video['path']).name
                   and d['file'].get('size') == video['size']
                   and d['file'].get('lastModified') == video['mtime_ns'] // 1000000]
        vid = self.state.data.get('video_id')
        if vid:
            matches = [d for d in matches if d['vid'] == vid]
        return matches[0] if len(matches) == 1 else None

    def verify_fields(self, p):
        if not same_text(p.get('title'), self.config['title']) or not same_text(p.get('description'), self.config['description']):
            raise AutomationError('TEXT_MISMATCH', 'Page title or description differs from input; submission stopped', exit_code=6,
                                  details={'actual_title': p.get('title'), 'actual_description': p.get('description')})
        privacy = p.get('visibilityActions', {}).get(VISIBILITY[self.config['visibility']])
        if not privacy or not privacy.get('checked'):
            raise AutomationError('VISIBILITY_MISMATCH', 'Page visibility differs from input; submission stopped', exit_code=6)

    def fill_fields(self):
        self.phase('metadata', 'Fill title, description, and visibility')
        for key in ['title', 'description']:
            p = self.poll(lambda p: p.get('actions', {}).get(key), self.config['step_timeout'], 'CONTROL_UNAVAILABLE', f'Cannot find the {key} input')
            value = self.config[key]
            if not same_text(p.get(key), value):
                self.mutate('fill_' + key, 'fill', target=p['actions'][key]['target'], value=value)
        p = self.probe()
        privacy = p.get('visibilityActions', {}).get(VISIBILITY[self.config['visibility']])
        if not privacy:
            raise AutomationError('CONTROL_UNAVAILABLE', 'Cannot uniquely identify visibility options; the page may have changed')
        if not privacy['checked']:
            self.mutate('set_visibility', 'set_checked', target=privacy['target'], checked=True)
        self.verify_fields(self.probe())
        # The site's editor adapter debounces CONTENT_CHANGE by 300 ms (trailing).
        # DOM text can be correct before React's saved textResult changes. Blur,
        # then require matching fields across a full second before any submission.
        self.mutate('blur_description', 'press', target=p['actions']['description']['target'], key='Tab')
        settled = self.poll(lambda p: same_text(p.get('title'), self.config['title'])
                            and same_text(p.get('description'), self.config['description'])
                            and (p.get('visibilityActions', {}).get(VISIBILITY[self.config['visibility']]) or {}).get('checked'),
                            self.config['step_timeout'], 'FORM_NOT_SETTLED', 'Editor values have not settled; submission stopped', stable_for=1)
        self.verify_fields(settled)

    def verify_saved_draft(self):
        self.phase('verify_draft', 'Reload the upload page and verify persisted draft video and text')
        p = self.open(UPLOAD_URL, lambda p: p.get('draftAvailable') or p.get('actions', {}).get('upload'))
        draft = self.owned_draft(p)
        if not p.get('draftAvailable') or not draft or not draft.get('uploaded'):
            raise AutomationError('DRAFT_UNVERIFIED', 'Cannot find the saved draft; inspect the retained tab before continuing', exit_code=7)
        if (not same_text(draft.get('title'), self.config['title'])
                or not same_text(draft.get('description'), self.config['description'])
                or draft.get('visibility') != VISIBILITY[self.config['visibility']]):
            raise AutomationError('DRAFT_CONTENT_MISMATCH', 'Persisted draft title, description, or visibility does not match', exit_code=7,
                                  details={'draft': draft})
        # Opening "continue editing" consumes this local cache. Leave the saved
        # slot intact; verify its persisted values after an actual page reload.
        self.state.save(status='completed', phase='draft_saved', evidence={'kind': 'persisted_local_draft', 'video_id': draft['vid'],
                        'title': draft['title'], 'description': draft['description'],
                        'scope': 'same Chrome profile and account; not a mobile/cloud draft'},
                        video_id=draft['vid'], result_url=UPLOAD_URL)

    def recent(self, search=None, limit=200):
        self.ensure_tab(platform='douyin', required_actions={'fill', 'press'}, min_revision=10)
        self.check_account()
        self.open(CONTENT_URL, lambda p: p.get('contentReady') and (p.get('contentItems') or p.get('listEnd')))
        if search:
            self.mutate('search_posts', 'fill', target={'selector':'input', 'name':'搜索作品', 'exact':True}, value=search)
            self.mutate('search_submit', 'press', target={'selector':'input', 'name':'搜索作品', 'exact':True}, key='Enter')
            self.sleep(1)
        seen, observed = {}, set()
        complete = False
        deadline=self.clock()+self.config['step_timeout']
        total=None
        while len(seen) < limit and self.clock()<deadline:
            p = self.probe();before=len(observed);total=p.get('total')
            for item in p.get('contentItems', []):
                key=observation_key(item);observed.add(key)
                if not search or search in item['text']:seen[key]=item
            if (p.get('listEnd') and (search or total is None or len(observed)>=total)) or (not search and isinstance(total,int) and len(observed)>=total):
                complete = True;break
            if len(observed)>before:deadline=self.clock()+self.config['step_timeout']
            scroll_list(self.client,self.tab_id,'[class^="info-title-text-"]')
            self.sleep(self.config['poll_interval'])
        return listing_result(self.state.data['account'],seen.values(),complete=complete,limit=limit,total=total)

    def set_schedule(self):
        wanted = self.config.get('schedule_at')
        if wanted:
            schedule_time(wanted, min_lead=7200, max_days=14)
            self.phase('schedule', 'Set platform publication time: ' + display_time(wanted))
            p = self.probe()
            if not p.get('scheduleChecked'):
                self.click('schedule')
            p = self.poll(lambda p: p.get('actions', {}).get('scheduleInput'), self.config['step_timeout'], 'SCHEDULE_UNAVAILABLE', 'Date/time input did not appear')
            # This controlled date input restores its previous value on Backspace.
            # Replace the selection directly, without an intermediate empty value.
            self.mutate('focus_schedule', 'press', target=p['actions']['scheduleInput']['target'], key='Home')
            platform = self.client.evaluate(self.tab_id, 'navigator.platform') or ''
            modifiers = 4 if 'Mac' in platform else 2
            for event in ['keyDown', 'keyUp']:
                params = {'type':event,'key':'a','code':'KeyA','windowsVirtualKeyCode':65,'modifiers':modifiers}
                if event == 'keyDown': params['commands'] = ['selectAll']
                self.mutate('select_schedule_' + event, 'cdp.send', method='Input.dispatchKeyEvent', params=params)
            self.mutate('replace_schedule', 'cdp.send', method='Input.insertText', params={'text':display_time(wanted)})
            self.mutate('commit_schedule', 'press', target=p['actions']['scheduleInput']['target'], key='Enter')
            self.mutate('blur_schedule', 'press', key='Tab')
            self.poll(lambda p: p.get('scheduleValue') == display_time(wanted) and p.get('scheduleChecked'),
                      self.config['step_timeout'], 'SCHEDULE_MISMATCH', 'Page schedule time was not applied correctly', stable_for=1)
        elif self.probe().get('scheduleChecked'):
            self.click('immediate')

    def verify_scheduled(self):
        wanted = self.config['schedule_at']
        date = display_time(wanted)
        year, month, day = date[:10].split('-')
        chinese_date = f'{year}年{month}月{day}日 {date[11:]}'
        self.open(CONTENT_URL, lambda p: p.get('contentItems'))
        def matches(p):
            return [x for x in p.get('contentItems', []) if self.config['title'] in x['text']
                    and (chinese_date in x['body'] or date in x['body']) and any(t in x['body'] for t in ['定时', '待发布', '审核中'])]
        p = self.poll(matches, self.config['step_timeout'], 'SCHEDULE_UNVERIFIED', 'Content list does not show the requested schedule; submission will not be repeated')
        items = matches(p)
        if len(items) != 1:
            raise AutomationError('SCHEDULE_UNVERIFIED', 'Multiple scheduled works have the same title; cannot verify uniquely', exit_code=7)
        self.state.save(status='completed', phase='scheduled', scheduled_at=wanted, result_url=CONTENT_URL,
                        evidence={'kind': 'scheduled_platform_row', 'item': items[0], 'schedule_at': wanted,
                                  'create_response': self.state.data.get('create_response'),
                                  'platform_status': '审核中' if '审核中' in items[0]['body'] else '待发布'})

    def submit_once(self, kind):
        # Prepare the target BEFORE intent: a missing control is known not to submit.
        p = self.poll(lambda p: (p.get('actions', {}).get(kind) or {}).get('enabled'), self.config['step_timeout'],
                      'CONTROL_UNAVAILABLE', f'No available {kind} button; upload, cover, or required fields may be incomplete')
        self.verify_fields(p)
        if kind == 'post' and self.config.get('schedule_at'):
            schedule_time(self.config['schedule_at'], min_lead=7200, max_days=14)
            if not p.get('scheduleChecked') or p.get('scheduleValue') != display_time(self.config['schedule_at']):
                raise AutomationError('SCHEDULE_MISMATCH', 'Schedule differs before submission; publication refused', exit_code=6)
        self.state.save(submit_intent={'kind': kind, 'at': time.time()})
        try:
            self.mutate('submit_' + kind, 'click', target=p['actions'][kind]['target'])
        except AutomationError as error:
            if error.code == 'ACTION_NOT_PERFORMED':
                self.state.save(submit_intent=None)
            raise

    def verify_post(self):
        # The UI uses clickable divs with no durable item IDs. Observe the browser's
        # own create response instead of treating navigation or a toast as proof.
        if self.config.get('schedule_at') and self.state.data.get('create_response'):
            return self.verify_scheduled()
        self.phase('verify_post', 'Verify the Douyin publication response without submitting again')
        intent = self.state.data['submit_intent']
        deadline = self.clock() + self.config['step_timeout']
        last_read_error = None
        while self.clock() < deadline:
            self.probe()  # login, verification, dialogs and visible rejection
            try:
                events = self.client.events(self.tab_id, since=int(intent['at'] * 1000) - 1000,
                                            methods=['Network.responseReceived'])
                for event in events:
                    params = event.get('eventParams', {})
                    response = params.get('response', {})
                    url = urlparse(response.get('url', ''))
                    if url.hostname != 'creator.douyin.com' or url.path.rstrip('/') not in {
                            '/web/api/media/aweme/create', '/web/api/media/aweme/create_v2'}:
                        continue
                    raw = self.client.cdp(self.tab_id, 'Network.getResponseBody', {'requestId': params['requestId']})
                    body = base64.b64decode(raw['body']).decode() if raw.get('base64Encoded') else raw['body']
                    payload = json.loads(body)
                    status = payload.get('status_code')
                    if status is not None and status != 0:
                        raise AutomationError('POST_REJECTED', 'Douyin rejected publication: ' + str(payload.get('status_msg') or payload.get('message') or status), exit_code=6)
                    containers = [payload, payload.get('aweme', {}), payload.get('data', {}), payload.get('item', {})]
                    post_id = next((str(c.get('aweme_id') or c.get('item_id')) for c in containers
                                    if isinstance(c, dict) and re.fullmatch(r'\d{10,}', str(c.get('aweme_id') or c.get('item_id') or ''))), None)
                    if status == 0 and response.get('status') == 200 and post_id:
                        if self.config.get('schedule_at'):
                            self.state.save(post_id=post_id, create_response={'status_code': 0, 'post_id': post_id})
                            self.verify_scheduled()
                            return
                        self.state.save(status='completed', phase='published', post_id=post_id,
                                        result_url='https://www.douyin.com/video/' + post_id,
                                        evidence={'kind': 'browser_create_response', 'status_code': 0, 'post_id': post_id})
                        return
                last_read_error = None
            except (APIError, TransportError, ValueError, KeyError) as error:
                last_read_error = str(error)
            self.sleep(self.config['poll_interval'])
        raise AutomationError('POST_UNVERIFIED', 'No successful response with a work ID; events expire after 3 minutes. Inspect the content list; resume only verifies',
                              exit_code=7, details={'last_read_error': last_read_error})

    def run(self):
        self.ensure_tab(platform='douyin', required_actions={'fill', 'insert_text', 'upload_files', 'click', 'press', 'set_checked', 'set_tab_policy'}, min_revision=10)
        # Any final intent is reconciled, never submitted again.
        if self.state.data.get('submit_intent'):
            self.check_account()
            if self.state.data['submit_intent']['kind'] == 'draft':
                self.verify_saved_draft()
            else:
                self.verify_post()
            return
        # Preserve an in-flight editor on resume. Read it before navigating away.
        p = self.probe()
        in_editor = p.get('editor') and self.state.data.get('upload_selected')
        if not in_editor:
            self.check_account()
        else:
            # Check the current account in an independent read tab, without losing the upload.
            original = self.tab_id
            check = self.client.create_tab(auto_close=False, meta={'job_id': self.state.data['job_id'], 'platform': 'douyin-account-check'})
            try:
                self.tab_id = check['tabId']
                self.check_account()
            finally:
                self.tab_id = original
                with contextlib.suppress(Exception):
                    self.client.close_tab(check['tabId'])
        # One profile-wide lock in main prevents concurrent jobs competing for the single draft slot.
        if not in_editor:
            p = self.open(UPLOAD_URL, lambda p: p.get('draftAvailable') or p.get('actions', {}).get('upload'))
            if p.get('draftAvailable'):
                draft = self.owned_draft(p)
                if not draft or not (self.config.get('reuse_draft') or self.state.data.get('video_id')):
                    raise AutomationError('EXISTING_DRAFT', 'An existing draft was preserved. Use --reuse-draft only if it belongs to the same file',
                                          exit_code=7, details={'drafts': p.get('drafts')})
                self.state.save(video_id=draft['vid'], upload_selected=True)
                self.click('continueDraft')
            else:
                if self.state.data.get('upload_selected'):
                    raise AutomationError('UPLOAD_PAGE_LOST', 'Upload page is lost and no matching draft exists; inspect before creating a new task', exit_code=7)
                video = fingerprint(self.config['video']['path'], quiet=True, code='INVALID_VIDEO')
                if video['sha256'] != self.config['video']['sha256'] or video['size'] != self.config['video']['size']:
                    raise AutomationError('VIDEO_CHANGED', 'Video changed after task creation; create a new task', exit_code=2)
                claim_key(self.state, self.config['state_dir'], self.config['video']['sha256'] + ':' + self.state.data['account'],
                          allow_duplicate=self.config.get('allow_duplicate', False))
                self.phase('upload', 'Select video and wait for upload and processing')
                self.state.save(upload_selected=True)
                try:
                    self.mutate('upload_video', 'upload_files', target=p['actions']['upload']['target'], files=[video['path']])
                except AutomationError as error:
                    if error.code == 'ACTION_NOT_PERFORMED':
                        self.state.save(upload_selected=False)
                    raise
        p = self.poll(lambda p: p.get('uploaded'), self.config['upload_timeout'], 'UPLOAD_TIMEOUT', 'Video upload or processing timed out; inspect the retained page, connection, and file')
        d = self.owned_draft(p)
        if d:
            self.state.save(video_id=d['vid'])
        self.fill_fields()
        mode = self.config['mode']
        if mode == 'upload':
            self.state.save(status='completed', phase='editor_ready', result_url=p['url'],
                            evidence={'kind': 'editor_verified', 'note': 'Editor retained; draft saving or publication has not been requested'})
            return
        if mode == 'draft':
            self.phase('save_draft', 'Save draft and leave (submit once)')
            self.submit_once('draft')
            self.poll(lambda p: not p.get('editor') and p.get('draftAvailable'), self.config['step_timeout'], 'DRAFT_UNVERIFIED', 'Draft save is unverified; saving will not be repeated')
            self.verify_saved_draft()
        else:
            self.set_schedule()
            self.phase('publish', 'Publish video (submit once)')
            self.submit_once('post')
            self.verify_post()


def parser_for_cli():
    p = argparse.ArgumentParser(prog='cli.douyin', description=__doc__)
    commands = p.add_subparsers(dest='command', required=True)
    def connection(q):
        q.add_argument('--api-url', '--base', help=f'Proxy API URL; default: {BASE}')
        q.add_argument('--json', action='store_true', help='Output single-line JSON')
        q.add_argument('--quiet', action='store_true', help='Suppress progress on stderr')
        q.add_argument('--keep-tab', action='store_true', help='Keep the tab on success; failures always retain it')
    for mode, help_text in [('upload', 'Upload, fill the editor, and keep the tab'), ('draft', 'Upload, save a draft, and reload to verify'), ('post', 'Upload and publish immediately')]:
        q = commands.add_parser(mode, help=help_text)
        connection(q)
        q.add_argument('--video', required=True, help='Local video path; MP4 recommended, at most 16 GiB')
        title = q.add_mutually_exclusive_group()
        title.add_argument('--title', help='Title of 1–30 characters; defaults to the filename stem')
        title.add_argument('--title-file', help='UTF-8 title file')
        desc = q.add_mutually_exclusive_group()
        desc.add_argument('--description', '--caption', help='Description of at most 1000 characters; may include hashtag text')
        desc.add_argument('--description-file', '--caption-file', help='UTF-8 description file; preserves newlines')
        q.add_argument('--visibility', choices=VISIBILITY, default='public', help='Visibility: public, friends, or private; default: public')
        q.add_argument('--account', help='Expected Douyin ID as displayed on the profile; stop on mismatch')
        q.add_argument('--reuse-draft', action='store_true', help='Reuse an existing draft matching the video filename, size, and modification time')
        q.add_argument('--allow-duplicate', action='store_true', help='Skip local file SHA-256 and account duplicate checks')
        q.add_argument('--upload-timeout', type=positive, default=1800, help='Upload/processing timeout in seconds; default: 1800')
        q.add_argument('--step-timeout', type=positive, default=90, help='Step timeout in seconds; default: 90')
        q.add_argument('--poll-interval', type=positive, default=2, help='Page polling interval in seconds; default: 2')
        q.add_argument('--state-dir', type=Path, default=DEFAULT_STATE_DIR, help='State and duplicate-index directory')
        q.add_argument('--state-file', type=Path, help='State file for a new task')
        if mode == 'post':
            q.add_argument('--schedule-at', help='Platform schedule: Shanghai time or ISO timestamp with timezone; 2 hours to 14 days ahead')
            q.add_argument('--draft', action='store_true', help='Save a draft instead; equivalent to the draft command')
    q = commands.add_parser('recent', help='Read works, including scheduled publications')
    connection(q)
    q.add_argument('--search', help='Search work descriptions')
    q.add_argument('--account', help='Expected Douyin ID')
    q.add_argument('--limit', type=int, default=200)
    q.add_argument('--status',choices=['scheduled','published','reviewing','rejected','draft','unknown'],help='Filter by observed platform publication status')
    q.add_argument('--require-complete',action='store_true',help='Return a nonzero exit code if the list is incomplete')
    q.add_argument('--step-timeout', type=positive, default=90)
    q.add_argument('--poll-interval', type=positive, default=1)
    q = commands.add_parser('resume', help='Resume a task; verify uncertain submissions without repeating them')
    connection(q)
    q.add_argument('--state-file', type=Path, required=True)
    q.add_argument('--reuse-draft', action='store_true', help='Explicitly allow reusing an existing draft for the same video file')
    q = commands.add_parser('status', help='Read local state without connecting to the browser')
    q.add_argument('--state-file', type=Path, required=True)
    q.add_argument('--json', action='store_true')
    return p


def summarize(state, error=None):
    d = state.data if state else {}
    result = {'success': error is None and d.get('status') == 'completed', 'status': d.get('status'), 'stage': d.get('phase'),
              'job_id': d.get('job_id'), 'mode': d.get('config', {}).get('mode'), 'account': d.get('account'),
              'tab_id': d.get('tab_id'), 'video_id': d.get('video_id'), 'post_id': d.get('post_id'), 'scheduled_at': d.get('scheduled_at'), 'url': d.get('result_url'),
              'state_file': str(state.path) if state else None, 'evidence': d.get('evidence')}
    if state:
        result['resume_command'] = shlex.join([sys.executable, str(Path(__file__).resolve().parent), 'resume', '--state-file', str(state.path)])
        if d.get('failure_screenshot'):
            result['failure_screenshot'] = d['failure_screenshot']
    if error:
        result.update(code=error.code, message=str(error), details=error.details)
    elif d.get('error'):
        result['error'] = d['error']
    return result


def main(argv=None):
    args = parser_for_cli().parse_args(argv)
    state = workflow = None
    exit_code = 0
    writable = False
    try:
        if args.command == 'recent':
            if not 1 <= args.limit <= 1000:
                raise AutomationError('INVALID_ARGUMENT', '--limit must be 1..1000', exit_code=2)
            memory = MemoryState({'job_id':uuid.uuid4().hex, 'config':{'account':args.account, 'step_timeout':args.step_timeout, 'poll_interval':args.poll_interval}})
            reader = DouyinWorkflow(BrowserClient(args.api_url or BASE), memory, quiet=args.quiet)
            try:
                result = select_publications(reader.recent(args.search,1000 if args.status else args.limit),args.status,args.limit)
                if args.require_complete and not result['complete']:raise AutomationError('LIST_INCOMPLETE','Content list is incomplete',exit_code=7,details=result)
            finally:
                if reader.tab_id:
                    with contextlib.suppress(Exception): reader.client.close_tab(reader.tab_id)
            print(json.dumps(result, ensure_ascii=False, indent=None if args.json else 2))
            return 0
        state = load_state(args.state_file) if args.command in {'status', 'resume'} else prepare(args)
        if args.command == 'status':
            result = summarize(state)
        else:
            # All CLI invocations serialize for a proxy/profile, even with different state dirs.
            import hashlib
            api_url = args.api_url or state.data.get('api_url') or BASE
            lock_key = hashlib.sha256(api_url.rstrip('/').encode()).hexdigest()[:16]
            profile_lock = DEFAULT_STATE_DIR / ('profile-' + lock_key + '.lock')
            with FileLock(profile_lock), FileLock(str(state.path) + '.lock'):
                if args.command == 'resume':
                    state = load_state(args.state_file)
                    if getattr(args, 'reuse_draft', False):
                        state.data['config']['reuse_draft'] = True
                if state.data.get('status') == 'completed':
                    result = summarize(state)
                else:
                    writable = True
                    state.save(status='running', error=None, api_url=api_url)
                    workflow = DouyinWorkflow(BrowserClient(api_url), state, quiet=args.quiet)
                    workflow.run()
                    result = summarize(state)
                    # Preserve the draft entry page and the upload editor for a user handoff.
                    if state.data['config']['mode'] == 'post' and not args.keep_tab:
                        try:
                            workflow.client.close_tab(workflow.tab_id)
                        except Exception as cleanup:
                            result['cleanup_warning'] = str(cleanup)
    except (Exception, KeyboardInterrupt) as caught:
        if isinstance(caught, KeyboardInterrupt):
            error = AutomationError('INTERRUPTED', 'Interrupted; state and tab retained for resume', exit_code=130)
        elif isinstance(caught, AutomationError):
            error = caught
        elif isinstance(caught, (APIError, TransportError)):
            error = AutomationError('API_UNAVAILABLE', str(caught), exit_code=7 if state and state.data.get('submit_intent') else 3)
        elif isinstance(caught, (OSError, ValueError)):
            error = AutomationError('LOCAL_IO_ERROR', str(caught), exit_code=2)
        else:
            error = AutomationError('UNEXPECTED_ERROR', f'{type(caught).__name__}: {caught}', exit_code=4)
        exit_code = error.exit_code
        if writable:
            with contextlib.suppress(Exception):
                state.save(status='failed', error={'code': error.code, 'message': str(error), 'details': error.details})
            if workflow:
                with contextlib.suppress(Exception):
                    workflow.diagnostics()
        result = summarize(state, error)
    print(json.dumps(result, ensure_ascii=False, indent=None if args.json else 2))
    return exit_code
