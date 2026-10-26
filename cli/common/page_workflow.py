"""Probe-driven workflow over one managed browser tab, shared by platform CLIs.

A platform supplies a read-only probe script (cli/<platform>/probe.js, run
with the shared DOM helpers via browser_client.load_probe) that returns the page state, including an
`actions` map of {target, enabled} controls. Probes are polled and retried
freely; every mutation is recorded as an intent first and never replayed, so
an uncertain outcome is reported instead of being repeated.
"""
import contextlib
import os
import re
import time

from .automation import AutomationError
from .browser_client import APIError, TransportError
from .media import log


def same_text(a, b):
    # Studio's description editor renders multi-line text with a trailing
    # newline of its own; compare values without it.
    norm = lambda v: (v or '').replace('\r\n', '\n').rstrip('\n')
    return norm(a) == norm(b)


URL_RE = re.compile(r'https?://\S+|(?<![\w@#])(?:[a-z0-9-]+\.)+[a-z]{2,}(?:/\S*)?', re.I)


def comparable_text(text):
    """Normalised post text for duplicate checks: links dropped (sites show them
    shortened), case folded, whitespace collapsed."""
    text = URL_RE.sub(' ', text or '')
    return ' '.join(text.replace('…', ' ').split()).casefold()


def same_published_text(posted, wanted, truncated=False):
    """Whether a published text is the wanted one; a text the site cut off
    ("Show more") matches by prefix."""
    a, b = comparable_text(posted), comparable_text(wanted)
    if not a or not b:
        return False
    return a == b or (truncated and b.startswith(a))


class MemoryState:
    """In-memory stand-in for TaskState, for one-shot commands with nothing to
    resume (reads, or an edit that is simply rerun)."""
    def __init__(self, data):
        self.data = data

    def save(self, **fields):
        self.data.update(fields)

    def intent(self, action):
        self.save(last_action={'name': action, 'outcome': 'pending'})

    def confirmed(self, **fields):
        self.save(last_action={**(self.data.get('last_action') or {}), 'outcome': 'confirmed'}, **fields)


class PageWorkflow:
    def __init__(self, client, state, probe_script, *, quiet=False, sleep=time.sleep, clock=time.monotonic):
        self.client, self.state = client, state
        self.config = state.data['config']
        self.probe_script = probe_script
        self.quiet = quiet
        self.sleep, self.clock = sleep, clock
        self.tab_id = state.data.get('tab_id')
        self.last_probe = None
        self.last_log = None

    def ensure_tab(self, *, platform, required_actions=(), min_revision=0, owner_key='job_id', on_lost=None):
        """Check the extension, then reuse this job's tab or open a new one.
        Returns True when a new tab was opened (any page state from before is gone).
        A tab recorded in the state but owned by another job is refused; when the
        recorded tab is gone, on_lost() runs before a new one is opened and may
        raise to stop (e.g. when the lost page held an unfinished upload)."""
        try:
            caps = self.client.capabilities()
        except (APIError, TransportError) as error:
            raise AutomationError('API_UNAVAILABLE', f'Cannot reach the proxy/extension: {error}', exit_code=3) from error
        missing = set(required_actions) - set(caps.get('actions', []))
        if missing or caps.get('implementationRevision', 0) < min_revision:
            raise AutomationError('EXTENSION_TOO_OLD', f'The loaded extension is too old (needs implementation revision ≥ {min_revision}'
                                  + (f", missing {', '.join(sorted(missing))}" if missing else '') + '); update and reload it',
                                  exit_code=3, details={'loaded_revision': caps.get('implementationRevision')})
        if self.tab_id:
            try:
                tab = self.client.tab(self.tab_id)
            except APIError as error:
                if error.status != 404:
                    raise
                tab = None
            if tab and tab.get('meta', {}).get(owner_key) != self.state.data['job_id']:
                raise AutomationError('TAB_OWNERSHIP_MISMATCH', 'The recorded tab belongs to another task; refusing to control it', exit_code=7)
            if not tab:
                self.tab_id = None
                if on_lost:
                    on_lost()
        opened = not self.tab_id
        if opened:
            tab = self.client.create_tab(auto_close=False, meta={owner_key: self.state.data['job_id'], 'platform': platform})
            self.tab_id = tab['tabId']
            self.state.save(tab_id=self.tab_id)
        self.client.action(self.tab_id, 'set_tab_policy', auto_close=False)
        return opened

    def navigate(self, url, *, attempts=3):
        """Load a page. Loading is a read, so a network failure is retried; a page
        that still cannot load stops the run with NAVIGATION_FAILED."""
        for attempt in range(attempts):
            try:
                return self.client.navigate(self.tab_id, url)
            except APIError as error:
                if error.code != 'NAVIGATION_FAILED':
                    raise
                if attempt == attempts - 1:
                    raise AutomationError('NAVIGATION_FAILED', f'Could not load {url}: {error} (network or proxy problem?); resume when it is back',
                                          exit_code=5, details={'url': url}) from error
                log(f'  loading failed ({error}); retrying', self.quiet)
                self.sleep(3 * (attempt + 1))

    def phase(self, name, message):
        self.state.save(phase=name, status='running')
        log(f'[{name}] {message}', self.quiet)

    def guard(self, probe):
        """Raise for page states that must stop the workflow (login, captcha, …)."""

    def observe(self, probe):
        """Record facts from a probe (e.g. the item id) after guard passes."""

    def probe(self):
        try:
            probe = self.client.evaluate(self.tab_id, self.probe_script)
        except APIError as error:
            if error.status == 404:
                raise AutomationError('TAB_LOST', 'The managed tab was closed or lost; performed actions are not replayed — resume to reconcile', exit_code=7) from error
            raise
        if not isinstance(probe, dict):
            raise AutomationError('PAGE_NOT_READY', 'The page is navigating; its state cannot be read yet')
        self.last_probe = probe
        self.guard(probe)
        self.observe(probe)
        return probe

    def poll(self, predicate, timeout, code, message, *, stable_for=0):
        """Wait for a read-only condition, optionally requiring it to hold across
        observations for stable_for seconds (e.g. a form's debounced updates).
        A failed read or false observation restarts the stability interval.
        """
        deadline = self.clock() + timeout
        last_error = None
        matched_since = None
        while self.clock() < deadline:
            try:
                probe = self.probe()
                result = predicate(probe)
                if result:
                    if matched_since is None:
                        matched_since = self.clock()
                    if self.clock() - matched_since >= stable_for:
                        return probe
                else:
                    matched_since = None
                progress = probe.get('progress', '')
                if progress and progress != self.last_log:
                    log('  ' + progress[:500], self.quiet)
                    self.last_log = progress
                last_error = None
            except AutomationError as error:
                if error.code != 'PAGE_NOT_READY':
                    raise
                matched_since = None
                last_error = str(error)
            except (APIError, TransportError) as error:
                # Only this read-only probe is retried. No click/file selection is replayed.
                matched_since = None
                last_error = str(error)
                if isinstance(error, APIError) and error.status == 404:
                    raise
            self.sleep(min(self.config['poll_interval'], max(0, deadline - self.clock())))
        details = {'last_read_error': last_error, 'progress': (self.last_probe or {}).get('progress', ''),
                   'alerts': (self.last_probe or {}).get('alerts', [])}
        raise AutomationError(code, message, exit_code=5, details=details)

    # Refusals that usually clear by themselves (an animation, an element being
    # re-rendered). Retrying them is safe: the action is known not to have run.
    TRANSIENT_REFUSALS = {'ELEMENT_OBSCURED', 'ELEMENT_NOT_VISIBLE', 'STALE_ELEMENT'}

    def mutate(self, name, action, *, attempts=3, **args):
        self.state.intent(name)
        try:
            for attempt in range(attempts):
                try:
                    result = (self.client.cdp(self.tab_id, **args) if action == 'cdp.send'
                              else self.client.action(self.tab_id, action, **args))
                    break
                except APIError as error:
                    if not (error.reason in self.TRANSIENT_REFUSALS and attempt < attempts - 1):
                        raise
                    self.sleep(1)
        except APIError as error:
            if error.not_performed:
                # Refused before any input reached the page: known not to have happened.
                self.state.save(last_action={**(self.state.data.get('last_action') or {}), 'outcome': 'not_performed',
                                             'reason': error.reason})
                raise AutomationError('ACTION_NOT_PERFORMED', f'{name} was not performed: {error.reason}', exit_code=4,
                                      details={'action': name, 'reason': error.reason}) from error
            raise AutomationError('ACTION_OUTCOME_UNKNOWN', f'{name}: no trustworthy result ({error}). Inspect the page and resume; do not repeat the action',
                                  exit_code=7, details={'action': name}) from error
        except TransportError as error:
            raise AutomationError('ACTION_OUTCOME_UNKNOWN', f'{name}: no trustworthy result ({error}). Inspect the page and resume; do not repeat the action',
                                  exit_code=7, details={'action': name}) from error
        self.state.confirmed()
        return result

    def click(self, key, *, timeout=None):
        probe = self.poll(lambda p: p.get('actions', {}).get(key) and p['actions'][key].get('enabled'),
                          timeout or self.config['step_timeout'], 'CONTROL_UNAVAILABLE', f'No usable {key} control: the UI may have changed or an earlier step is incomplete')
        return self.mutate('click_' + key, 'click', target=probe['actions'][key]['target'])

    def diagnostics(self):
        if not self.tab_id:
            return
        directory = self.state.path.parent / (self.state.path.stem + '-diagnostics')
        directory.mkdir(parents=True, exist_ok=True, mode=0o700)
        with contextlib.suppress(Exception):
            path = self.client.screenshot(self.tab_id, directory / 'failure.png')
            os.chmod(path, 0o600)
            self.state.save(failure_screenshot=str(path))
        if self.last_probe:
            self.state.save(last_observation=self.last_probe)
