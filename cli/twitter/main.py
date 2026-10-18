"""Post to X (Twitter) through x.com in your own Chrome, via the Browser Agent Proxy.

Examples (from the repository root; from elsewhere use `python3 /path/to/cli/twitter`):
  python3 -m cli.twitter post --text "Hello"
  python3 -m cli.twitter post --text-file post.txt --image a.jpg --image b.png
  python3 -m cli.twitter post --text "New episode" --video clip.mp4
  python3 -m cli.twitter recent --limit 10 --json
  python3 -m cli.twitter recent --text "Hello"          # duplicate check
  python3 -m cli.twitter resume --state-file /path/state.json

Progress goes to stderr; one JSON result goes to stdout. The Post button is
pressed at most once per job; an uncertain outcome is reconciled against the
profile timeline, never retried blindly.
"""
import argparse
import contextlib
import hashlib
import json
import os
from pathlib import Path
import re
import shlex
import sys
import time
import uuid

from ..common.browser_client import APIError, BASE, BrowserClient, TransportError, load_probe
from ..common.automation import AutomationError, FileLock, TaskState, claim_key
from ..common.media import fingerprint, log, read_text
from ..common.page_workflow import PageWorkflow, MemoryState, comparable_text, same_published_text, same_text

PROBE = load_probe(Path(__file__).parent / 'probe.js')
SITE = 'https://x.com'
SITE_HOSTS = {'x.com', 'twitter.com', 'mobile.x.com', 'mobile.twitter.com'}
DEFAULT_STATE_DIR = Path(os.environ.get('XDG_STATE_HOME', str(Path.home() / '.local' / 'state'))) / 'browser-agent' / 'twitter'
MIN_REVISION = 8
REQUIRED_ACTIONS = {'upload_files', 'fill', 'click', 'set_tab_policy'}

# x.com's own limits for one post (web composer).
IMAGE_TYPES = {'.jpg', '.jpeg', '.png', '.webp'}
GIF_TYPES = {'.gif'}
VIDEO_TYPES = {'.mp4', '.mov'}
MAX_IMAGES = 4
MAX_IMAGE_BYTES = 5 * 1024 * 1024
MAX_GIF_BYTES = 15 * 1024 * 1024
MAX_VIDEO_BYTES = 512 * 1024 * 1024


# ---------------------------------------------------------------------------
# Text and media checks (local, before the browser is touched)

# Links are dropped because x.com shows them shortened (t.co / truncated URLs).
comparable = comparable_text
same_post_text = same_published_text


def classify_media(images, video):
    """Validate the attachment set against x.com's rules; returns fingerprints."""
    images = list(images or [])
    if video and images:
        raise AutomationError('INVALID_MEDIA', 'A post can have up to 4 images, or 1 GIF, or 1 video — not a mix', exit_code=2)
    media = []
    gifs = [p for p in images if Path(p).suffix.lower() in GIF_TYPES]
    if gifs and len(images) > 1:
        raise AutomationError('INVALID_MEDIA', 'A GIF must be the only attachment', exit_code=2)
    if len(images) > MAX_IMAGES:
        raise AutomationError('INVALID_MEDIA', f'At most {MAX_IMAGES} images per post', exit_code=2)
    for path in images:
        suffix = Path(path).suffix.lower()
        if suffix not in IMAGE_TYPES | GIF_TYPES:
            raise AutomationError('INVALID_MEDIA', f'Unsupported image type {suffix or "(none)"}: use jpg, png, webp or gif', exit_code=2)
        info = fingerprint(path, quiet=True, code='INVALID_MEDIA')
        limit = MAX_GIF_BYTES if suffix in GIF_TYPES else MAX_IMAGE_BYTES
        if info['size'] > limit:
            raise AutomationError('INVALID_MEDIA', f'{Path(path).name} is over {limit // 1024 // 1024} MB', exit_code=2)
        media.append({**info, 'kind': 'gif' if suffix in GIF_TYPES else 'image'})
    if video:
        suffix = Path(video).suffix.lower()
        if suffix not in VIDEO_TYPES:
            raise AutomationError('INVALID_MEDIA', f'Unsupported video type {suffix or "(none)"}: use mp4 or mov', exit_code=2)
        info = fingerprint(video, quiet=True, code='INVALID_MEDIA')
        if info['size'] > MAX_VIDEO_BYTES:
            raise AutomationError('INVALID_MEDIA', f'{Path(video).name} is over {MAX_VIDEO_BYTES // 1024 // 1024} MB', exit_code=2)
        media.append({**info, 'kind': 'video'})
    return media


def content_key(text, media):
    digest = hashlib.sha256()
    digest.update(comparable(text).encode('utf-8'))
    for item in media:
        digest.update(b'\0' + item['sha256'].encode())
    return digest.hexdigest()


def validate_state(data):
    config = data['config']
    if not isinstance(config.get('text'), str) or not isinstance(config.get('media'), list):
        raise AutomationError('INVALID_STATE', 'Invalid post content in state', exit_code=2)
    for item in config['media']:
        if not isinstance(item, dict) or not isinstance(item.get('path'), str) or item.get('kind') not in {'image', 'gif', 'video'} \
                or not re.fullmatch('[0-9a-f]{64}', str(item.get('sha256'))):
            raise AutomationError('INVALID_STATE', 'Invalid media fingerprint in state', exit_code=2)
    if not isinstance(config.get('state_dir'), str):
        raise AutomationError('INVALID_STATE', 'Invalid state_dir', exit_code=2)


def load_state(path):
    return TaskState.load(path, platform='twitter', validate=validate_state)


# ---------------------------------------------------------------------------
# Workflow

class TwitterWorkflow(PageWorkflow):
    def __init__(self, client, state, *, quiet=False, site=SITE, sleep=time.sleep, clock=time.monotonic):
        super().__init__(client, state, PROBE, quiet=quiet, sleep=sleep, clock=clock)
        self.site = site.rstrip('/')

    def guard(self, probe):
        if probe.get('challenge'):
            raise AutomationError('VERIFICATION_REQUIRED', 'x.com is asking for a captcha or verification; complete it in the kept tab, then resume', exit_code=3)
        if probe.get('accountLocked'):
            raise AutomationError('ACCOUNT_LOCKED', 'x.com reports the account is locked or restricted (/account/access); resolve it in the kept tab', exit_code=3)
        if probe.get('loginRequired'):
            raise AutomationError('LOGIN_REQUIRED', 'Chrome is not signed in to x.com; sign in in the kept tab, then resume', exit_code=3)
        host = (probe.get('host') or '').lower()
        if host and host not in SITE_HOSTS:
            raise AutomationError('UNEXPECTED_ORIGIN', f'The page left x.com: {host}', exit_code=3)

    def attach(self):
        self.ensure_tab(platform='twitter', required_actions=REQUIRED_ACTIONS, min_revision=MIN_REVISION)
        self.phase('account', 'Checking sign-in and account')
        self.navigate(self.site + '/home')
        probe = self.poll(lambda p: p.get('loggedIn'), self.config['step_timeout'], 'SITE_NOT_READY',
                          'x.com did not finish loading a signed-in page (network, sign-in or an unsupported UI)')
        handle = probe['handle']
        expected = (self.config.get('account') or self.state.data.get('account') or '').lstrip('@')
        if expected and handle.lower() != expected.lower():
            raise AutomationError('ACCOUNT_MISMATCH', f'Signed in as @{handle}, expected @{expected}', exit_code=3)
        self.state.save(account=handle)
        return handle

    # -- reading the profile timeline ------------------------------------------------

    def recent_posts(self, limit=20, settle=20):
        """The signed-in account's newest posts from its profile (own posts only;
        reposts of others are skipped). Scrolls until `limit` or no more load."""
        handle = self.state.data['account']
        self.navigate(f'{self.site}/{handle}')
        try:
            self.poll(lambda p: p['timeline']['ready'] and p['path'].strip('/').lower() == handle.lower(),
                      settle, 'TIMELINE_NOT_READY', '')
        except AutomationError as error:
            if error.code != 'TIMELINE_NOT_READY':
                raise
            return []
        seen, stale = {}, 0
        while len(seen) < limit and stale < 3:
            probe = self.probe()
            before = len(seen)
            for post in probe.get('posts', []):
                if post['author'].lower() == handle.lower():
                    seen.setdefault(post['id'], post)
            if probe['timeline'].get('empty'):
                break
            stale = stale + 1 if len(seen) == before else 0
            if len(seen) >= limit:
                break
            self.client.evaluate(self.tab_id, 'window.scrollBy(0, window.innerHeight * 2), true')
            self.sleep(1.5)
        posts = sorted(seen.values(), key=lambda p: p.get('datetime') or '', reverse=True)
        return [{'id': p['id'], 'url': f"{self.site}/{p['author']}/status/{p['id']}", 'datetime': p.get('datetime'),
                 'text': p.get('text', ''), 'truncated': p.get('truncated', False),
                 'pinned': p.get('socialContext', False), 'images': p.get('images', 0), 'video': p.get('video', False)}
                for p in posts[:limit]]

    def check_not_posted(self):
        text = self.config['text']
        if not comparable(text):
            return  # a media-only post has no text to compare; the local index still applies
        self.phase('duplicate_check', 'Checking recent posts for the same text')
        matches = [p for p in self.recent_posts(limit=self.config.get('duplicate_window', 20))
                   if same_post_text(p['text'], text, p['truncated'])]
        if matches:
            raise AutomationError('DUPLICATE_POST', 'A recent post already has this text; pass --allow-duplicate to post anyway',
                                  exit_code=7, details={'matches': matches})

    # -- composing ------------------------------------------------------------------

    def open_composer(self):
        self.phase('compose', 'Opening the post composer')
        self.navigate(self.site + '/compose/post')
        self.poll(lambda p: (p.get('composer') or {}).get('text') and p['composer'].get('fileInput'),
                  self.config['step_timeout'], 'COMPOSER_UNAVAILABLE', 'The post composer did not open')

    def fill_text(self):
        text = self.config['text']
        if not text:
            return
        probe = self.probe()
        box = probe['composer']['text']
        if not same_text(box['value'], text):
            self.mutate('fill_text', 'fill', target=box['target'], value=text)
        box = (self.probe().get('composer') or {}).get('text')
        if not box or not same_text(box['value'], text):
            raise AutomationError('TEXT_VERIFICATION_FAILED', 'The composer does not contain the requested text after filling it')

    def attach_media(self):
        media = self.config['media']
        if not media:
            return
        self.phase('media', f'Attaching {len(media)} file(s)')
        for item in media:
            path = Path(item['path'])
            if not path.is_file() or path.stat().st_size != item['size'] or path.stat().st_mtime_ns != item['mtime_ns']:
                raise AutomationError('MEDIA_CHANGED', f'{path.name} changed or disappeared after the job was prepared', exit_code=2)
        probe = self.probe()
        target = probe['composer']['fileInput']['target']
        selected = self.mutate('select_media', 'upload_files', target=target, files=[m['path'] for m in media])
        entries = selected.get('files', [])
        if sorted((e.get('name'), e.get('size')) for e in entries) != sorted((Path(m['path']).name, m['size']) for m in media):
            raise AutomationError('FILE_SELECTION_MISMATCH', 'Chrome selected different files than requested', exit_code=7)
        expected = {'image': 0, 'video': 0}
        for item in media:
            expected['video' if item['kind'] == 'video' else 'image'] += 1

        def ready(p):
            composer = p.get('composer') or {}
            if composer.get('alerts'):
                raise AutomationError('MEDIA_REJECTED', 'x.com rejected an attachment: ' + ' | '.join(composer['alerts'])[:1000],
                                      exit_code=6, details={'alerts': composer['alerts']})
            m = composer.get('media') or {}
            counted = m.get('images', 0) >= expected['image'] and m.get('videos', 0) >= expected['video']
            return counted and not m.get('uploading') and (composer.get('post') or {}).get('enabled')
        self.phase('processing', 'Waiting for x.com to finish uploading/processing the media')
        self.poll(ready, self.config['media_timeout'], 'MEDIA_TIMEOUT',
                  'The attachments did not finish uploading in time (or x.com did not accept them)')
        self.state.save(media_attached=True)

    def close_autocomplete(self, probe):
        """A hashtag or mention at the end of the text leaves x.com's suggestion
        list open, and its overlay covers the Post button. Escape closes it.
        Returns a fresh probe when it had to act, else the one given."""
        composer = probe.get('composer') or {}
        if not composer.get('autocomplete'):
            return probe
        self.mutate('close_autocomplete', 'press', target=composer['text']['target'], key='Escape')

        def closed(p):
            if p.get('sheet'):
                raise AutomationError('COMPOSER_CLOSING', 'Escape asked to discard the draft instead of closing the suggestions',
                                      exit_code=4, details={'sheet': p['sheet']})
            c = p.get('composer')
            if not c:
                raise AutomationError('COMPOSER_LOST', 'The composer closed while dismissing the suggestions', exit_code=4)
            return not c.get('autocomplete')
        self.poll(closed, self.config['step_timeout'], 'AUTOCOMPLETE_STUCK', 'The suggestion list did not close')
        text = self.config['text']
        probe = self.probe()
        box = (probe.get('composer') or {}).get('text')
        if text and (not box or not same_text(box['value'], text)):
            raise AutomationError('TEXT_VERIFICATION_FAILED', 'The text changed while dismissing the suggestions')
        return probe

    def publish(self):
        probe = self.close_autocomplete(self.probe())
        post = (probe.get('composer') or {}).get('post')
        if not post or not post.get('enabled'):
            raise AutomationError('POST_BLOCKED', 'The Post button is disabled (text over the limit for this account, or media not accepted)',
                                  exit_code=6, details={'composer': probe.get('composer')})
        before = {t['text'] for t in probe.get('toasts', [])}
        self.phase('posting', 'Pressing Post (exactly once) and waiting for the result')
        self.state.save(post_intent=True, post_intent_at=time.time())
        try:
            self.mutate('post', 'click', target=post['target'])
        except AutomationError as error:
            if error.code == 'ACTION_NOT_PERFORMED':
                # The extension refused before clicking: nothing was posted, so a
                # resume may compose and press Post again.
                self.state.save(post_intent=False)
            raise

        def outcome(p):
            for toast in p.get('toasts', []):
                if toast.get('statusId'):
                    return True
            composer = p.get('composer')
            if not composer:
                return True  # the composer closed: x.com accepted the post
            fresh = [t['text'] for t in p.get('toasts', []) if t['text'] not in before]
            if (fresh or composer.get('alerts')) and (composer.get('post') or {}).get('enabled'):
                raise AutomationError('POST_REJECTED', 'x.com did not accept the post: ' + ' | '.join(fresh + composer.get('alerts', []))[:1000],
                                      exit_code=6, details={'messages': fresh + composer.get('alerts', [])})
            return False
        probe = self.poll(outcome, self.config['step_timeout'], 'POST_OUTCOME_UNKNOWN',
                          'Post was pressed but no result was seen; run resume to reconcile against the profile')
        status = next((t for t in probe.get('toasts', []) if t.get('statusId')), None)
        if status:
            self.state.save(post_id=status['statusId'])
        self.verify_posted()

    def verify_posted(self):
        """Find the post on the profile: by ID when x.com reported one, else the
        newest own post since the intent whose text (or media) matches."""
        self.phase('verify', 'Verifying the post on the profile')
        text, media = self.config['text'], self.config['media']
        since = self.state.data.get('post_intent_at', 0) - 120
        deadline = self.clock() + self.config['step_timeout']
        while True:
            posts = self.recent_posts(limit=10)
            post_id = self.state.data.get('post_id')
            for post in posts:
                if post_id and post['id'] != post_id:
                    continue
                if not post_id:
                    when = _epoch(post.get('datetime'))
                    if when is not None and when < since:
                        continue
                    if text and not same_post_text(post['text'], text, post['truncated']):
                        continue
                    if not _media_matches(post, media):
                        continue
                self.state.save(phase='posted', status='completed', post_id=post['id'], post_url=post['url'],
                                evidence={'post': post, 'text_matches': not text or same_post_text(post['text'], text, post['truncated'])})
                return
            if self.clock() >= deadline:
                raise AutomationError('POST_UNVERIFIED', 'The post could not be found on the profile; check it by hand',
                                      exit_code=7, details={'post_id': post_id, 'recent': posts[:5]})
            self.sleep(self.config['poll_interval'] * 2)

    def run(self):
        self.attach()
        if self.state.data.get('status') == 'completed':
            return
        if self.state.data.get('post_intent'):
            # Reconcile an uncertain post; never press Post again.
            self.verify_posted()
            return
        if not self.config.get('allow_duplicate'):
            self.check_not_posted()
        claim_key(self.state, self.config['state_dir'], content_key(self.config['text'], self.config['media']) + ':' + self.state.data['account'],
                  allow_duplicate=self.config.get('allow_duplicate', False), index_name='post-index')
        self.open_composer()
        self.fill_text()
        self.attach_media()
        self.fill_text()  # some editors drop text when media is attached; re-verify
        self.publish()


def _media_matches(post, media):
    """Attachment kinds and counts as the timeline shows them (a GIF plays as video)."""
    if any(m['kind'] in {'video', 'gif'} for m in media):
        return post.get('video', False)
    return post.get('images', 0) == len(media) and not post.get('video', False)


def _epoch(value):
    if not value:
        return None
    with contextlib.suppress(ValueError):
        from datetime import datetime
        return datetime.fromisoformat(value.replace('Z', '+00:00')).timestamp()
    return None


# ---------------------------------------------------------------------------
# Command line

def positive(name):
    def parse(value):
        try:
            number = float(value)
        except ValueError:
            raise argparse.ArgumentTypeError(f'{name} must be a number')
        if not 0 < number < float('inf'):
            raise argparse.ArgumentTypeError(f'{name} must be a positive number')
        return number
    return parse


def add_connection(parser):
    parser.add_argument('--api-url', '--base', default=None, help=f'Browser Agent Proxy URL (default {BASE})')
    parser.add_argument('--quiet', action='store_true', help='no progress on stderr')
    parser.add_argument('--json', action='store_true', help='print the result JSON on one line')


def add_timing(parser):
    parser.add_argument('--step-timeout', type=positive('--step-timeout'), default=60, help='seconds for each page step (default 60)')
    parser.add_argument('--poll-interval', type=positive('--poll-interval'), default=2, help='seconds between page reads (default 2)')


def parser_for_cli():
    parser = argparse.ArgumentParser(prog='cli.twitter', description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = parser.add_subparsers(dest='command', required=True)

    post = sub.add_parser('post', help='publish a post: text, images (≤4), a GIF or a video',
                          description='Publish one post. Text is optional when media is attached.')
    add_connection(post)
    text = post.add_mutually_exclusive_group()
    text.add_argument('--text', help='post text')
    text.add_argument('--text-file', help='UTF-8 file with the post text (line breaks kept)')
    post.add_argument('--image', action='append', default=[], metavar='PATH',
                      help='attach an image (jpg/png/webp ≤5 MB; repeat up to 4) or one GIF (≤15 MB)')
    post.add_argument('--video', metavar='PATH', help='attach one video (mp4/mov ≤512 MB); not combinable with images')
    post.add_argument('--account', help='expected signed-in handle (without @); refuse to post from another account')
    post.add_argument('--allow-duplicate', action='store_true', help='post even if a recent post has the same text or this job content was posted before')
    post.add_argument('--duplicate-window', type=int, default=20, help='how many recent posts the duplicate check reads (default 20)')
    post.add_argument('--media-timeout', type=positive('--media-timeout'), default=900, help='seconds to wait for media upload/processing (default 900)')
    add_timing(post)
    post.add_argument('--state-dir', type=Path, default=DEFAULT_STATE_DIR, help='job state and duplicate index directory')
    post.add_argument('--state-file', type=Path, help='explicit job state JSON path')
    post.add_argument('--keep-tab', action='store_true', help='keep the tab open after success (always kept on failure)')

    recent = sub.add_parser('recent', help="list the signed-in account's newest posts (for duplicate checks)")
    add_connection(recent)
    recent.add_argument('--limit', type=int, default=20, help='number of posts (default 20)')
    recent.add_argument('--text', help='also report whether a listed post has this text (links, case and spacing ignored)')
    recent.add_argument('--account', help='expected signed-in handle (without @)')
    add_timing(recent)

    resume = sub.add_parser('resume', help='continue a job; after Post was pressed it only reconciles, never posts again')
    add_connection(resume)
    resume.add_argument('--state-file', type=Path, required=True, help='job state JSON')
    resume.add_argument('--keep-tab', action='store_true', help='keep the tab open after success')

    status = sub.add_parser('status', help='print a job state without contacting the browser')
    status.add_argument('--state-file', type=Path, required=True, help='job state JSON')
    status.add_argument('--json', action='store_true', help='print the result JSON on one line')
    return parser


def prepare_post(args):
    text = read_text(args.text, args.text_file).strip('\n')
    if args.text is None and args.text_file is None and not args.image and not args.video:
        raise AutomationError('EMPTY_POST', 'Give --text/--text-file, media (--image/--video), or both', exit_code=2)
    if not text.strip() and not args.image and not args.video:
        raise AutomationError('EMPTY_POST', 'The text is empty and no media is attached', exit_code=2)
    if '\x00' in text:
        raise AutomationError('INVALID_TEXT', 'The text contains a NUL character', exit_code=2)
    if args.account and not re.fullmatch(r'@?\w{1,15}', args.account):
        raise AutomationError('INVALID_ACCOUNT', '--account must be an X handle (letters, digits, _; up to 15)', exit_code=2)
    if not 1 <= args.duplicate_window <= 200:
        raise AutomationError('INVALID_ARGUMENT', '--duplicate-window must be between 1 and 200', exit_code=2)
    media = classify_media(args.image, args.video)
    root = args.state_dir.expanduser().resolve()
    job_id = uuid.uuid4().hex
    state_path = args.state_file.expanduser().resolve() if args.state_file else root / job_id / 'state.json'
    if state_path.exists():
        raise AutomationError('STATE_EXISTS', 'The state file already exists; use resume or another path', exit_code=2)
    config = {'text': text, 'media': media, 'account': (args.account or '').lstrip('@') or None,
              'allow_duplicate': args.allow_duplicate, 'duplicate_window': args.duplicate_window,
              'media_timeout': args.media_timeout, 'step_timeout': args.step_timeout,
              'poll_interval': args.poll_interval, 'state_dir': str(root)}
    return TaskState(state_path, {'schema_version': 1, 'platform': 'twitter', 'job_id': job_id, 'created_at': time.time(),
                                  'phase': 'prepared', 'status': 'prepared', 'config': config, 'api_url': args.api_url or BASE})


def summarize(state, *, error=None):
    data = state.data if state else {}
    result = {'success': error is None and data.get('status') == 'completed', 'status': data.get('status'),
              'stage': data.get('phase'), 'job_id': data.get('job_id'), 'account': data.get('account'),
              'post_id': data.get('post_id'), 'post_url': data.get('post_url'),
              'state_file': str(state.path) if state else None, 'evidence': data.get('evidence')}
    if error:
        result.update(code=error.code, message=str(error), details=error.details)
    if state:
        result['resume_command'] = shlex.join([sys.executable, str(Path(__file__).resolve().parent), 'resume',
                                               '--state-file', str(state.path)])
    return result


def run_recent(args):
    if not 1 <= args.limit <= 200:
        raise AutomationError('INVALID_ARGUMENT', '--limit must be between 1 and 200', exit_code=2)
    state = MemoryState({'job_id': 'recent-' + uuid.uuid4().hex,
                         'config': {'account': (args.account or '').lstrip('@') or None,
                                    'step_timeout': args.step_timeout, 'poll_interval': args.poll_interval}})
    client = BrowserClient(args.api_url or BASE)
    workflow = TwitterWorkflow(client, state, quiet=args.quiet)
    try:
        handle = workflow.attach()
        log(f'Reading @{handle} recent posts', args.quiet)
        posts = workflow.recent_posts(limit=args.limit)
    finally:
        if workflow.tab_id:
            with contextlib.suppress(Exception):
                client.close_tab(workflow.tab_id)
    result = {'success': True, 'account': handle, 'posts': posts}
    if args.text is not None:
        result['text'] = args.text
        result['text_matches'] = [p for p in posts if same_post_text(p['text'], args.text, p['truncated'])]
        result['duplicate'] = bool(result['text_matches'])
    return result


def main(argv=None):
    args = parser_for_cli().parse_args(argv)
    state = workflow = lock = None
    writable = False
    exit_code = 0
    try:
        if args.command == 'recent':
            print(json.dumps(run_recent(args), ensure_ascii=False, indent=None if args.json else 2))
            return 0
        if args.command in {'status', 'resume'}:
            state = load_state(args.state_file)
            if args.command == 'status' or state.data.get('status') == 'completed':
                print(json.dumps(summarize(state), ensure_ascii=False, indent=None if args.json else 2))
                return 0
        else:
            state = prepare_post(args)
        lock = FileLock(str(state.path) + '.lock')
        lock.__enter__()
        if args.command == 'resume':
            state = load_state(args.state_file)
        writable = True
        api_url = args.api_url or state.data.get('api_url') or BASE
        state.save(status='running', error=None, api_url=api_url)
        workflow = TwitterWorkflow(BrowserClient(api_url), state, quiet=args.quiet)
        workflow.run()
        result = summarize(state)
        if not args.keep_tab and workflow.tab_id:
            try:
                workflow.client.close_tab(workflow.tab_id)
                state.save(tab_closed=True)
            except Exception as cleanup_error:
                result['cleanup_warning'] = str(cleanup_error)
    except KeyboardInterrupt:
        error = AutomationError('INTERRUPTED', 'Interrupted; the job and its tab are kept — resume to continue', exit_code=130)
        exit_code = 130
        if writable:
            with contextlib.suppress(Exception):
                state.save(status='interrupted', error={'code': error.code, 'message': str(error)})
        result = summarize(state, error=error)
    except Exception as caught:
        if isinstance(caught, AutomationError):
            error = caught
        elif isinstance(caught, (APIError, TransportError)):
            error = AutomationError('BROWSER_API_FAILED', str(caught), exit_code=7 if state and state.data.get('post_intent') else 3)
        else:
            error = AutomationError('UNEXPECTED_ERROR', f'{type(caught).__name__}: {caught}', exit_code=4)
        exit_code = error.exit_code
        if writable:
            with contextlib.suppress(Exception):
                state.save(status='failed', error={'code': error.code, 'message': str(error), 'details': error.details})
            if workflow and error.code == 'DUPLICATE_POST':
                # Nothing was typed or posted, so there is no page worth keeping.
                with contextlib.suppress(Exception):
                    workflow.client.close_tab(workflow.tab_id)
                    state.save(tab_closed=True)
            elif workflow:
                with contextlib.suppress(Exception):
                    workflow.diagnostics()
        result = summarize(state, error=error)
    finally:
        if lock and lock.stream and not lock.stream.closed:
            lock.__exit__()
    print(json.dumps(result, ensure_ascii=False, indent=None if getattr(args, 'json', False) else 2))
    return exit_code
