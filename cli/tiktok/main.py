"""Post a video to TikTok through TikTok Studio in your own Chrome, via the Browser Agent Proxy.

Examples (from the repository root; from elsewhere use `python3 /path/to/cli/tiktok`):
  python3 -m cli.tiktok post --video clip.mp4 --caption "Why prices end in 9" --hashtag MoneyBrain --hashtag Psychology
  python3 -m cli.tiktok post --video clip.mp4 --caption-file caption.txt --visibility private --ai-generated
  python3 -m cli.tiktok recent --limit 10 --json
  python3 -m cli.tiktok recent --caption "Why prices end in 9"     # duplicate check
  python3 -m cli.tiktok resume --state-file /path/state.json

Progress goes to stderr; one JSON result goes to stdout. Post is pressed at
most once per job; an uncertain outcome is reconciled against TikTok Studio's
content list, never retried blindly.
"""
import argparse
import contextlib
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
from ..common.page_workflow import PageWorkflow, MemoryState, comparable_text, same_published_text

PROBE = load_probe(Path(__file__).parent / 'probe.js')
SITE = 'https://www.tiktok.com'
UPLOAD_URL = SITE + '/tiktokstudio/upload?from=webapp&tab=video'
CONTENT_URL = SITE + '/tiktokstudio/content'
DRAFTS_URL = CONTENT_URL + '?tab=draft'
SITE_HOSTS = {'www.tiktok.com', 'tiktok.com'}
DEFAULT_STATE_DIR = Path(os.environ.get('XDG_STATE_HOME', str(Path.home() / '.local' / 'state'))) / 'browser-agent' / 'tiktok'
MIN_REVISION = 9
REQUIRED_ACTIONS = {'upload_files', 'fill', 'insert_text', 'press', 'click', 'set_tab_policy'}

VIDEO_TYPES = {'.mp4', '.mov', '.webm', '.m4v', '.avi', '.mkv', '.flv', '.3gp', '.mpeg', '.mpg'}
MAX_VIDEO_BYTES = 30 * 1024 ** 3  # TikTok Studio's own upload limit
MAX_CAPTION = 4000
# privacy_level values of TikTok's visibility options (data-value on each option).
VISIBILITY = {'everyone': '0', 'friends': '2', 'private': '1'}
HASHTAG_RE = re.compile(r'(#\w+)')


# ---------------------------------------------------------------------------
# Local checks (before the browser is touched)

def build_caption(text, hashtags):
    """The caption as it will be typed: the text, then any --hashtag tags."""
    tags = []
    for tag in hashtags or []:
        tag = tag.strip()
        tag = tag if tag.startswith('#') else '#' + tag
        if not re.fullmatch(r'#\w+', tag):
            raise AutomationError('INVALID_HASHTAG', f'Not a hashtag: {tag!r} (letters, digits and _ only)', exit_code=2)
        tags.append(tag)
    text = (text or '').strip()
    if tags:
        text = (text + ('\n\n' if text else '') + ' '.join(tags)).strip()
    return text


def caption_segments(caption):
    """Split a caption into ('text', …) and ('tag', '#…') parts, in order."""
    return [('tag' if HASHTAG_RE.fullmatch(part) else 'text', part) for part in HASHTAG_RE.split(caption) if part]


def post_time(video_id):
    """TikTok ids carry their creation time (unix seconds) in the top 32 bits."""
    try:
        return int(video_id) >> 32
    except (TypeError, ValueError):
        return None


def validate_state(data):
    config = data['config']
    video = config.get('video')
    if not isinstance(config.get('caption'), str) or not isinstance(video, dict) or not isinstance(video.get('path'), str) \
            or not re.fullmatch('[0-9a-f]{64}', str(video.get('sha256'))):
        raise AutomationError('INVALID_STATE', 'Invalid video or caption in state', exit_code=2)
    if config.get('visibility') not in VISIBILITY or not isinstance(config.get('state_dir'), str):
        raise AutomationError('INVALID_STATE', 'Invalid visibility or state_dir', exit_code=2)


def load_state(path):
    return TaskState.load(path, platform='tiktok', validate=validate_state)


# ---------------------------------------------------------------------------
# Workflow

class TikTokWorkflow(PageWorkflow):
    def __init__(self, client, state, *, quiet=False, sleep=time.sleep, clock=time.monotonic):
        super().__init__(client, state, PROBE, quiet=quiet, sleep=sleep, clock=clock)

    def guard(self, probe):
        if probe.get('challenge'):
            raise AutomationError('VERIFICATION_REQUIRED', 'TikTok is showing a captcha or verification; complete it in the kept tab, then resume', exit_code=3)
        if probe.get('loginRequired'):
            raise AutomationError('LOGIN_REQUIRED', 'Chrome is not signed in to TikTok; sign in in the kept tab, then resume', exit_code=3)
        host = (probe.get('host') or '').lower()
        if host and host not in SITE_HOSTS:
            raise AutomationError('UNEXPECTED_ORIGIN', f'The page left tiktok.com: {host}', exit_code=3)

    def open_page(self, url, ready, code, message):
        self.navigate(url)
        return self.poll(ready, self.config['step_timeout'], code, message)

    def attach(self):
        self.ensure_tab(platform='tiktok', required_actions=REQUIRED_ACTIONS, min_revision=MIN_REVISION)
        self.phase('account', 'Checking sign-in and account')
        probe = self.open_page(CONTENT_URL, lambda p: p.get('loggedIn') and (p.get('content') or {}).get('ready'),
                               'SITE_NOT_READY', 'TikTok Studio did not finish loading a signed-in page (network, sign-in or an unsupported UI)')
        handle = probe['handle']
        expected = (self.config.get('account') or self.state.data.get('account') or '').lstrip('@')
        if expected and handle.lower() != expected.lower():
            raise AutomationError('ACCOUNT_MISMATCH', f'Signed in as @{handle}, expected @{expected}', exit_code=3)
        self.state.save(account=handle)
        return handle

    # -- reading the content list ----------------------------------------------------

    def recent_posts(self, limit=20):
        """The account's newest posts from TikTok Studio's content list (own posts
        only), scrolling the virtualised table until `limit` or the end."""
        handle = self.state.data['account']
        probe = self.open_page(CONTENT_URL, lambda p: (p.get('content') or {}).get('ready'),
                               'CONTENT_NOT_READY', 'The TikTok Studio content list did not load')
        seen, stale = {}, 0
        while len(seen) < limit and stale < 3:
            before = len(seen)
            for post in probe.get('posts', []):
                if post['author'].lower() == handle.lower():
                    seen.setdefault(post['id'], post)
            if (probe.get('content') or {}).get('empty') or len(seen) >= limit:
                break
            stale = stale + 1 if len(seen) == before else 0
            self.client.evaluate(self.tab_id, '(() => { const a = [...document.querySelectorAll(\'a[href*="/video/"],a[href*="/photo/"]\')].pop();'
                                              ' if (a) a.scrollIntoView({block: "end"}); window.scrollBy(0, window.innerHeight); return true; })()')
            self.sleep(1.5)
            probe = self.probe()
        posts = sorted(seen.values(), key=lambda p: int(p['id']), reverse=True)[:limit]
        return [{'id': p['id'], 'url': f"{SITE}/@{p['author']}/{p['kind']}/{p['id']}", 'kind': p['kind'],
                 'created_at': post_time(p['id']), 'caption': p['text'], 'privacy': p.get('privacy')} for p in posts]

    def recent_drafts(self):
        """The account's drafts (TikTok keeps up to 30), newest first. Drafts have
        no public id; each is identified by its caption."""
        zero_since = []

        def loaded(p):
            listing = p.get('draftList') or {}
            if not listing.get('ready'):
                return False
            if listing['count']:
                return True
            # The count reads 0 until the list has loaded; trust 0 only once it holds.
            zero_since.append(self.clock())
            return self.clock() - zero_since[0] >= 4
        probe = self.open_page(DRAFTS_URL, loaded, 'DRAFTS_NOT_READY', 'The TikTok Studio draft list did not load')
        count = probe['draftList']['count']
        with contextlib.suppress(AutomationError):  # rows render after the count; wait briefly for all of them
            probe = self.poll(lambda p: len(p.get('drafts', [])) >= count, 10, 'DRAFTS_PARTIAL', '')
        return [{'caption': d['text'], 'row': d['row']} for d in probe.get('drafts', [])]

    def check_not_posted(self):
        caption = self.config['caption']
        if not comparable_text(caption):
            return  # nothing to compare; the local index still applies
        self.phase('duplicate_check', 'Checking recent posts and drafts for the same caption')
        matches = [p for p in self.recent_posts(limit=self.config.get('duplicate_window', 20))
                   if same_published_text(p['caption'], caption)]
        drafts = [d for d in self.recent_drafts() if same_published_text(d['caption'], caption)]
        if matches or drafts:
            where = 'a recent post' if matches else 'a draft'
            raise AutomationError('DUPLICATE_POST', f'{where.capitalize()} already has this caption; pass --allow-duplicate to post anyway',
                                  exit_code=7, details={'matches': matches, 'drafts': drafts})

    # -- page chrome ------------------------------------------------------------------

    def clear_tour(self, probe=None):
        """Close a product tour (react-joyride) whose overlay covers the form."""
        probe = probe or self.probe()
        if not probe.get('tour'):
            return probe
        self.mutate('close_tour', 'press', key='Escape')
        return self.poll(lambda p: not p.get('tour'), self.config['step_timeout'], 'TOUR_STUCK',
                         'A TikTok Studio product tour stayed open over the form')

    def refuse_modal(self, probe, where):
        if probe.get('modals'):
            raise AutomationError('UNEXPECTED_DIALOG', f'TikTok Studio opened a dialog {where}: ' + probe['modals'][0]['text'],
                                  exit_code=4, details={'modals': probe['modals']})

    def form(self, probe):
        return probe.get('upload') or {}

    # -- uploading ----------------------------------------------------------------------

    def upload_video(self):
        video = self.config['video']
        path = Path(video['path'])
        if not path.is_file() or path.stat().st_size != video['size'] or path.stat().st_mtime_ns != video['mtime_ns']:
            raise AutomationError('VIDEO_CHANGED', f'{path.name} changed or disappeared after the job was prepared', exit_code=2)
        self.phase('upload', f'Opening TikTok Studio and selecting {path.name}')
        probe = self.open_page(UPLOAD_URL, lambda p: self.form(p).get('fileInput') and self.form(p).get('selectStage') or p.get('modals'),
                               'UPLOAD_PAGE_UNAVAILABLE', 'The TikTok Studio upload page did not offer a file picker')
        probe = self.clear_tour(probe)
        self.refuse_modal(probe, 'before the upload')
        selected = self.mutate('select_video', 'upload_files', target=self.form(probe)['fileInput']['target'], files=[str(path)])
        names = [(f.get('name'), f.get('size')) for f in selected.get('files', [])]
        if names != [(path.name, video['size'])]:
            raise AutomationError('FILE_SELECTION_MISMATCH', 'Chrome selected a different file than requested', exit_code=7)
        self.state.save(upload_started=True)

        def uploaded(p):
            self.refuse_modal(p, 'during the upload')
            status = self.form(p).get('status') or {}
            if status.get('failed'):
                raise AutomationError('UPLOAD_FAILED', 'TikTok rejected the upload: ' + status.get('text', ''), exit_code=6,
                                      details={'status': status, 'alerts': p.get('alerts')})
            if status.get('percent') is not None and status['percent'] < 100:
                p['progress'] = f"uploading {status['percent']:.0f}%"
            return status.get('uploaded') and (self.form(p).get('caption') or {}).get('target')
        self.phase('processing', 'Waiting for TikTok to receive the video')
        self.poll(uploaded, self.config['upload_timeout'], 'UPLOAD_TIMEOUT', 'The video did not finish uploading in time')
        self.state.save(uploaded=True)

    # -- the caption ----------------------------------------------------------------------

    def caption_box(self):
        probe = self.clear_tour()
        box = self.form(probe).get('caption')
        if not box or not box.get('enabled'):
            raise AutomationError('CAPTION_UNAVAILABLE', 'The caption editor is missing or disabled', exit_code=4)
        return probe, box

    def add_hashtag(self, tag):
        """Type a hashtag and pick it from TikTok's suggestion list — only then does
        it become a real hashtag (typed text alone stays plain). The list's first,
        focused entry is always the typed tag itself, even for a brand-new one."""
        _, box = self.caption_box()
        before = len(box.get('tags', []))
        self.mutate('type_hashtag', 'insert_text', target=box['target'], value=tag)

        def offered(p):
            s = self.form(p).get('suggestions')
            return s and s['items'] and s['items'][0].casefold() == tag.casefold() and s.get('focused', 0) in (0, -1)
        try:
            self.poll(offered, min(20, self.config['step_timeout']), 'HASHTAG_NOT_OFFERED', '')
        except AutomationError as error:
            if error.code != 'HASHTAG_NOT_OFFERED':
                raise
            self.close_suggestions()
            self.state.save(plain_hashtags=self.state.data.get('plain_hashtags', []) + [tag])
            log(f'  {tag} was not offered as a hashtag; it stays plain text', self.quiet)
            return
        self.mutate('pick_hashtag', 'press', target=box['target'], key='Enter')
        self.poll(lambda p: len((self.form(p).get('caption') or {}).get('tags', [])) > before and not self.form(p).get('suggestions'),
                  self.config['step_timeout'], 'HASHTAG_NOT_PICKED', f'{tag} was not turned into a hashtag')

    def close_suggestions(self):
        probe = self.probe()
        if not self.form(probe).get('suggestions'):
            return
        self.mutate('close_suggestions', 'press', target=self.form(probe)['caption']['target'], key='Escape')
        self.poll(lambda p: not self.form(p).get('suggestions'), self.config['step_timeout'], 'SUGGESTIONS_STUCK',
                  'The hashtag suggestion list did not close')

    def write_caption(self):
        caption = self.config['caption']
        self.phase('caption', 'Writing the caption')
        segments = caption_segments(caption)
        first = segments.pop(0)[1] if segments and segments[0][0] == 'text' else ''
        _, box = self.caption_box()
        self.mutate('fill_caption', 'fill', target=box['target'], value=first)
        for kind, part in segments:
            if kind == 'tag':
                self.add_hashtag(part)
                continue
            _, box = self.caption_box()
            if box['value'].endswith((' ', '\xa0')) and part[:1] == ' ':
                part = part[1:]  # picking a hashtag already added the space after it
            if part:
                self.mutate('type_caption', 'insert_text', target=box['target'], value=part)
        self.close_suggestions()
        self.verify_caption()

    def verify_caption(self):
        _, box = self.caption_box()
        if comparable_text(box['value']) != comparable_text(self.config['caption']):
            raise AutomationError('CAPTION_VERIFICATION_FAILED', 'The caption editor does not contain the requested caption',
                                  details={'value': box['value'][:500]})
        if box.get('limit') and box.get('length') and box['length'] > box['limit']:
            raise AutomationError('CAPTION_TOO_LONG', f"TikTok counts {box['length']} of {box['limit']} characters", exit_code=2)

    # -- settings -----------------------------------------------------------------------

    def set_visibility(self):
        wanted = VISIBILITY[self.config['visibility']]
        self.phase('settings', f"Setting who can watch: {self.config['visibility']}")
        probe = self.clear_tour()
        trigger = self.form(probe).get('visibility')
        if not trigger:
            raise AutomationError('VISIBILITY_UNAVAILABLE', 'The "who can watch" control is missing', exit_code=4)
        if not trigger.get('open'):
            self.mutate('open_visibility', 'click', target=trigger['target'])
        probe = self.poll(lambda p: any(o['value'] == wanted for o in (self.form(p).get('visibility') or {}).get('options', [])),
                          self.config['step_timeout'], 'VISIBILITY_OPTION_MISSING', 'TikTok did not offer the requested visibility')
        option = next(o for o in self.form(probe)['visibility']['options'] if o['value'] == wanted)
        self.mutate('pick_visibility', 'click', target=option['target'])
        self.poll(lambda p: not (self.form(p).get('visibility') or {}).get('open')
                  and (self.form(p).get('visibility') or {}).get('text') == option['text'],
                  self.config['step_timeout'], 'VISIBILITY_NOT_SET', 'The visibility did not change to the requested option')

    def set_ai_label(self):
        """Set TikTok's "AI-generated content" label to what the job asked for
        (off unless --ai-generated), whatever the page started with."""
        wanted = bool(self.config.get('ai_generated'))
        probe = self.clear_tour()
        form = self.form(probe)
        aigc = form.get('aigc')
        if aigc and aigc.get('checked') == wanted:
            return
        if not aigc and not wanted:
            return  # the setting is not offered (or still folded away, where it starts off)
        if (form.get('advanced') or {}).get('collapsed'):
            self.mutate('show_more_settings', 'click', target=form['advanced']['toggle']['target'])
            probe = self.poll(lambda p: (self.form(p).get('aigc') or {}).get('toggle'), self.config['step_timeout'],
                              'AI_LABEL_UNAVAILABLE', 'The "AI-generated content" setting did not appear')
            aigc = self.form(probe).get('aigc')
            if aigc.get('checked') == wanted:
                return
        if not aigc or not aigc.get('toggle') or aigc.get('disabled'):
            raise AutomationError('AI_LABEL_UNAVAILABLE', 'The "AI-generated content" setting is missing or disabled', exit_code=4)
        self.mutate('label_ai_generated' if wanted else 'unlabel_ai_generated', 'click', target=aigc['toggle']['target'])
        is_set = lambda p: (self.form(p).get('aigc') or {}).get('checked') == wanted
        # Turning it on, TikTok explains the label in a dialog whose primary button confirms.
        probe = self.poll(lambda p: p.get('modals') or is_set(p), self.config['step_timeout'],
                          'AI_LABEL_NOT_SET', 'The AI-generated label did not change')
        if probe.get('modals'):
            modal = probe['modals'][0]
            if not wanted or not modal.get('primary'):
                self.refuse_modal(probe, 'while changing the AI-generated label')
            self.mutate('confirm_ai_label', 'click', target=modal['primary']['target'])
        self.poll(lambda p: not p.get('modals') and is_set(p), self.config['step_timeout'],
                  'AI_LABEL_NOT_SET', 'The AI-generated label did not change')

    # -- posting ----------------------------------------------------------------------

    def publish(self):
        """Press Post — or Save draft in draft mode — exactly once."""
        draft = self.config.get('draft')
        probe = self.clear_tour()
        self.refuse_modal(probe, 'before posting')
        button = self.form(probe).get('draft' if draft else 'post')
        if not button or not button.get('enabled') or button.get('loading'):
            raise AutomationError('POST_BLOCKED', f"The {'Save draft' if draft else 'Post'} button is disabled", exit_code=6,
                                  details={'upload': self.form(probe), 'alerts': probe.get('alerts')})
        if draft:
            self.phase('saving_draft', 'Pressing Save draft (exactly once) and waiting for the result')
        else:
            self.phase('posting', 'Pressing Post (exactly once) and waiting for the result')
        self.state.save(post_intent=True, post_intent_at=time.time())
        try:
            self.mutate('save_draft' if draft else 'post', 'click', target=button['target'])
        except AutomationError as error:
            if error.code == 'ACTION_NOT_PERFORMED':
                self.state.save(post_intent=False)  # refused before clicking: nothing was posted
            raise
        answered = []

        def outcome(p):
            if not (p.get('path') or '').startswith('/tiktokstudio/upload'):
                return True  # TikTok leaves the upload page once it accepts the post
            if p.get('modals'):
                modal = p['modals'][0]
                if answered and modal['text'] == answered[0]:
                    return False  # the answered dialog is still closing
                if modal.get('primary') and modal.get('neutral') and not answered:
                    # A question in reply to Post ("post now?"): the job asked to post, so go on, once.
                    answered.append(modal['text'])
                    self.mutate('confirm_post', 'click', target=modal['primary']['target'])
                    return False
                # Still on the upload page with a message: TikTok did not take the post.
                # A resume may try again; its duplicate check still sees a post that did go out.
                self.state.save(post_intent=False)
                raise AutomationError('POST_REJECTED', 'TikTok did not accept the post: ' + modal['text'], exit_code=6,
                                      details={'modals': p['modals'], 'answered': answered})
            return False
        self.poll(outcome, self.config['step_timeout'], 'POST_OUTCOME_UNKNOWN',
                  'The button was pressed but no result was seen; run resume to reconcile against the content list')
        if answered:
            self.state.save(confirmed_dialogs=answered)
        self.verify_posted()

    def verify_draft(self):
        """Find the saved draft by its caption (drafts have no id)."""
        self.phase('verify', 'Verifying the draft in the draft list')
        deadline = self.clock() + self.config.get('verify_timeout', 180)
        caption = self.config['caption']
        while True:
            drafts = self.recent_drafts()
            found = [d for d in drafts if same_published_text(d['caption'], caption)] if comparable_text(caption) else drafts[:1]
            if found:
                self.state.save(phase='drafted', status='completed', post_url=DRAFTS_URL, evidence={'draft': found[0]})
                return
            if self.clock() >= deadline:
                raise AutomationError('DRAFT_UNVERIFIED', 'The draft could not be found in the draft list; check it by hand',
                                      exit_code=7, details={'drafts': drafts[:5]})
            self.sleep(self.config['poll_interval'] * 3)

    def verify_posted(self):
        """Find the post in TikTok Studio's content list: the newest own post
        created since the attempt whose caption matches."""
        if self.config.get('draft'):
            return self.verify_draft()
        self.phase('verify', 'Verifying the post in the content list')
        since = self.state.data.get('post_intent_at', 0) - 120
        deadline = self.clock() + self.config.get('verify_timeout', 180)
        caption = self.config['caption']
        while True:
            posts = self.recent_posts(limit=10)
            for post in posts:
                if (post['created_at'] or 0) < since:
                    continue
                if comparable_text(caption) and not same_published_text(post['caption'], caption):
                    continue
                self.state.save(phase='posted', status='completed', post_id=post['id'], post_url=post['url'],
                                evidence={'post': post})
                return
            if self.clock() >= deadline:
                raise AutomationError('POST_UNVERIFIED', 'The post could not be found in the content list; check it by hand',
                                      exit_code=7, details={'recent': posts[:5]})
            self.sleep(self.config['poll_interval'] * 3)

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
        claim_key(self.state, self.config['state_dir'], self.config['video']['sha256'] + ':' + self.state.data['account'],
                  allow_duplicate=self.config.get('allow_duplicate', False), index_name='post-index')
        self.upload_video()
        self.write_caption()
        self.set_visibility()
        self.set_ai_label()
        self.verify_caption()  # settings must not have touched it
        self.publish()


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
    parser = argparse.ArgumentParser(prog='cli.tiktok', description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = parser.add_subparsers(dest='command', required=True)

    post = sub.add_parser('post', help='publish one video with a caption',
                          description='Publish one video through TikTok Studio. #hashtags in the caption become real hashtags.')
    add_connection(post)
    post.add_argument('--video', required=True, metavar='PATH', help='the video file (mp4 recommended; ≤30 GB)')
    caption = post.add_mutually_exclusive_group()
    caption.add_argument('--caption', help='caption text; #hashtags in it are picked from TikTok\'s suggestions')
    caption.add_argument('--caption-file', help='UTF-8 file with the caption (line breaks kept)')
    post.add_argument('--hashtag', action='append', default=[], metavar='TAG',
                      help='append a hashtag (with or without #); repeatable')
    post.add_argument('--visibility', choices=sorted(VISIBILITY), default='everyone', help='who can watch (default everyone)')
    post.add_argument('--ai-generated', action='store_true', help='label the video as AI-generated (TikTok\'s "AI-generated content" setting); off when omitted')
    post.add_argument('--draft', action='store_true', help='save as a draft instead of posting (finish and post it later yourself)')
    post.add_argument('--account', help='expected signed-in handle (without @); refuse to post from another account')
    post.add_argument('--allow-duplicate', action='store_true', help='post even if a recent post has the same caption or this video was posted before')
    post.add_argument('--duplicate-window', type=int, default=20, help='how many recent posts the duplicate check reads (default 20)')
    post.add_argument('--upload-timeout', type=positive('--upload-timeout'), default=1800, help='seconds to wait for the upload (default 1800)')
    add_timing(post)
    post.add_argument('--state-dir', type=Path, default=DEFAULT_STATE_DIR, help='job state and duplicate index directory')
    post.add_argument('--state-file', type=Path, help='explicit job state JSON path')
    post.add_argument('--keep-tab', action='store_true', help='keep the tab open after success (always kept on failure)')

    recent = sub.add_parser('recent', help="list the signed-in account's newest posts (for duplicate checks)")
    add_connection(recent)
    recent.add_argument('--limit', type=int, default=20, help='number of posts (default 20)')
    recent.add_argument('--caption', help='also report whether a listed post has this caption (links, case and spacing ignored)')
    recent.add_argument('--account', help='expected signed-in handle (without @)')
    recent.add_argument('--drafts', action='store_true', help='also list the drafts (they have no id; matched by caption)')
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
    text = read_text(args.caption, args.caption_file) if (args.caption is not None or args.caption_file is not None) else ''
    caption = build_caption(text, args.hashtag)
    if '\x00' in caption:
        raise AutomationError('INVALID_CAPTION', 'The caption contains a NUL character', exit_code=2)
    if len(caption) > MAX_CAPTION:
        raise AutomationError('CAPTION_TOO_LONG', f'The caption is {len(caption)} characters; TikTok allows {MAX_CAPTION}', exit_code=2)
    if args.account and not re.fullmatch(r'@?[\w.]{2,24}', args.account):
        raise AutomationError('INVALID_ACCOUNT', '--account must be a TikTok handle (letters, digits, _ and .)', exit_code=2)
    if not 1 <= args.duplicate_window <= 200:
        raise AutomationError('INVALID_ARGUMENT', '--duplicate-window must be between 1 and 200', exit_code=2)
    suffix = Path(args.video).suffix.lower()
    if suffix not in VIDEO_TYPES:
        raise AutomationError('INVALID_VIDEO', f'Unsupported video type {suffix or "(none)"}: use mp4 (or mov/webm)', exit_code=2)
    video = fingerprint(args.video, quiet=True, code='INVALID_VIDEO')
    if video['size'] > MAX_VIDEO_BYTES:
        raise AutomationError('INVALID_VIDEO', 'The video is over 30 GB', exit_code=2)
    root = args.state_dir.expanduser().resolve()
    job_id = uuid.uuid4().hex
    state_path = args.state_file.expanduser().resolve() if args.state_file else root / job_id / 'state.json'
    if state_path.exists():
        raise AutomationError('STATE_EXISTS', 'The state file already exists; use resume or another path', exit_code=2)
    config = {'caption': caption, 'video': video, 'visibility': args.visibility, 'ai_generated': args.ai_generated, 'draft': args.draft,
              'account': (args.account or '').lstrip('@') or None, 'allow_duplicate': args.allow_duplicate,
              'duplicate_window': args.duplicate_window, 'upload_timeout': args.upload_timeout,
              'step_timeout': args.step_timeout, 'poll_interval': args.poll_interval, 'state_dir': str(root)}
    return TaskState(state_path, {'schema_version': 1, 'platform': 'tiktok', 'job_id': job_id, 'created_at': time.time(),
                                  'phase': 'prepared', 'status': 'prepared', 'config': config, 'api_url': args.api_url or BASE})


def summarize(state, *, error=None):
    data = state.data if state else {}
    result = {'success': error is None and data.get('status') == 'completed', 'status': data.get('status'),
              'stage': data.get('phase'), 'job_id': data.get('job_id'), 'account': data.get('account'),
              'post_id': data.get('post_id'), 'post_url': data.get('post_url'),
              'state_file': str(state.path) if state else None, 'evidence': data.get('evidence')}
    if data.get('plain_hashtags'):
        result['plain_hashtags'] = data['plain_hashtags']
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
    workflow = TikTokWorkflow(client, state, quiet=args.quiet)
    try:
        handle = workflow.attach()
        log(f'Reading @{handle} recent posts', args.quiet)
        posts = workflow.recent_posts(limit=args.limit)
        drafts = workflow.recent_drafts() if args.drafts or args.caption is not None else None
    finally:
        if workflow.tab_id:
            with contextlib.suppress(Exception):
                client.close_tab(workflow.tab_id)
    result = {'success': True, 'account': handle, 'posts': posts}
    if args.drafts:
        result['drafts'] = drafts
    if args.caption is not None:
        result['caption'] = args.caption
        result['caption_matches'] = [p for p in posts if same_published_text(p['caption'], args.caption)]
        result['draft_matches'] = [d for d in drafts if same_published_text(d['caption'], args.caption)]
        result['duplicate'] = bool(result['caption_matches'] or result['draft_matches'])
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
        workflow = TikTokWorkflow(BrowserClient(api_url), state, quiet=args.quiet)
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
            if workflow and error.code in {'DUPLICATE_POST', 'DUPLICATE_UPLOAD'}:
                # Nothing was uploaded, so there is no page worth keeping.
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
