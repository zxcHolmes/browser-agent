"""Probe-driven workflow over one managed browser tab, shared by platform CLIs.

A platform supplies a read-only probe script (cli/<platform>/probe.js, run
with the shared DOM helpers via browser_client.load_probe) that returns the page state, including an
`actions` map of {target, enabled} controls. Probes are polled and retried
freely; every mutation is recorded as an intent first and never replayed, so
an uncertain outcome is reported instead of being repeated.
"""
import contextlib
import os
import time

from .automation import AutomationError
from .browser_client import APIError, TransportError
from .media import log


def same_text(a, b):
    # Studio's description editor renders multi-line text with a trailing
    # newline of its own; compare values without it.
    norm = lambda v: (v or '').replace('\r\n', '\n').rstrip('\n')
    return norm(a) == norm(b)


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
                raise AutomationError('TAB_LOST', '受管标签已关闭或丢失；已执行的动作不会重放，请使用 resume 核验', exit_code=7) from error
            raise
        if not isinstance(probe, dict):
            raise AutomationError('PAGE_NOT_READY', '页面正在切换，暂时无法读取页面状态')
        self.last_probe = probe
        self.guard(probe)
        self.observe(probe)
        return probe

    def poll(self, predicate, timeout, code, message):
        deadline = self.clock() + timeout
        last_error = None
        while self.clock() < deadline:
            try:
                probe = self.probe()
                result = predicate(probe)
                if result:
                    return probe
                progress = probe.get('progress', '')
                if progress and progress != self.last_log:
                    log('  ' + progress[:500], self.quiet)
                    self.last_log = progress
                last_error = None
            except AutomationError as error:
                if error.code != 'PAGE_NOT_READY':
                    raise
                last_error = str(error)
            except (APIError, TransportError) as error:
                # Only this read-only probe is retried. No click/file selection is replayed.
                last_error = str(error)
                if isinstance(error, APIError) and error.status == 404:
                    raise
            self.sleep(min(self.config['poll_interval'], max(0, deadline - self.clock())))
        details = {'last_read_error': last_error, 'progress': (self.last_probe or {}).get('progress', ''),
                   'alerts': (self.last_probe or {}).get('alerts', [])}
        raise AutomationError(code, message, exit_code=5, details=details)

    def mutate(self, name, action, **args):
        self.state.intent(name)
        try:
            result = self.client.action(self.tab_id, action, **args)
        except (APIError, TransportError) as error:
            raise AutomationError('ACTION_OUTCOME_UNKNOWN', f'{name} 未获得可信完成结果：{error}。请检查现有页面后恢复，不要重复操作',
                                  exit_code=7, details={'action': name}) from error
        self.state.confirmed()
        return result

    def click(self, key, *, timeout=None):
        probe = self.poll(lambda p: p.get('actions', {}).get(key) and p['actions'][key].get('enabled'),
                          timeout or self.config['step_timeout'], 'CONTROL_UNAVAILABLE', f'找不到可操作的 {key} 控件，界面可能变化或前置步骤未完成')
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
