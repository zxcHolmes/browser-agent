import json
from datetime import datetime,timedelta
from cli.common.scheduling import ZONE
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from cli.common.automation import AutomationError, TaskState
from cli.common.browser_client import APIError, TransportError, BrowserClient
from cli.douyin.main import DouyinWorkflow, parser_for_cli, prepare, load_state, main, VISIBILITY


class Clock:
    def __init__(self): self.value = 0
    def __call__(self): return self.value
    def sleep(self, value): self.value += value


class Browser:
    def __init__(self, state):
        self.state = state
        self.path = 'about:blank'
        self.calls = []
        self.saved = None
        self.file = None
        self.title = self.description = ''
        self.privacy = '0'
        self.uploaded = False
        self.submit_error = None
        self.reject = False
        self.rev = 10
        self.login = False
        self.counter = 10

    def capabilities(self):
        return {'implementationRevision': self.rev, 'actions': ['fill', 'insert_text', 'click', 'press', 'upload_files', 'set_checked', 'set_tab_policy']}

    def create_tab(self, **kwargs):
        self.counter += 1
        return {'tabId': self.counter}

    def tab(self, tab): return {'meta': {'job_id': self.state.data['job_id']}}
    def close_tab(self, tab): pass

    def navigate(self, tab, url):
        self.path = url
        return {}

    def evaluate(self, tab, script):
        editor = '/content/post/video' in self.path
        home = self.path.endswith('/home')
        p = {'host': 'creator.douyin.com', 'path': self.path, 'url': self.path, 'loggedIn': not self.login,
             'account': '12345' if home and not self.login else None, 'loginRequired': self.login,
             'editor': editor, 'uploaded': editor and self.uploaded, 'draftAvailable': bool(self.saved) and not editor,
             'drafts': [self.saved] if self.saved and not editor else [], 'title': self.title, 'description': self.description,
             'rejected': self.reject, 'alerts': ['上传失败'] if self.reject else [],
             'visibilityActions': {v: {'checked': self.privacy == v, 'target': 'privacy-' + v} for v in VISIBILITY.values()},
             'actions': {'upload': {'enabled': True, 'target': 'file'}, 'title': {'enabled': True, 'target': 'title'},
                         'description': {'enabled': True, 'target': 'description'}, 'draft': {'enabled': True, 'target': 'draft'},
                         'post': {'enabled': True, 'target': 'post'}, 'continueDraft': {'enabled': True, 'target': 'continue'}}}
        return p

    def action(self, tab, action, **args):
        self.calls.append((action, args))
        target = args.get('target')
        if action == 'upload_files':
            v = self.state.data['config']['video']
            self.file = {'name': Path(v['path']).name, 'size': v['size'], 'lastModified': v['mtime_ns'] // 1000000}
            self.uploaded = True
            self.path = 'https://creator.douyin.com/creator-micro/content/post/video'
        if action == 'fill': setattr(self, target, args['value'])
        if action == 'set_checked': self.privacy = target[-1]
        if action == 'click':
            if self.submit_error and target in {'draft', 'post'}: raise self.submit_error
            if target == 'draft':
                self.saved = {'file': self.file, 'vid': 'v-test', 'title': self.title, 'description': self.description,
                              'visibility': self.privacy, 'uploaded': True, 'uid': 'uid'}
                self.path = 'https://creator.douyin.com/creator-micro/content/upload'
            if target == 'continue':
                self.file = self.saved['file']
                self.uploaded = True
                self.path = 'https://creator.douyin.com/creator-micro/content/post/video'
        return {}

    def events(self, tab, **kwargs):
        return [{'eventParams': {'requestId': 'request1', 'response': {'url': 'https://creator.douyin.com/web/api/media/aweme/create_v2', 'status': 200}}}]

    def cdp(self, *args):
        return {'body': json.dumps({'status_code': 0, 'aweme': {'aweme_id': '1234567890123456789'}})}


class DouyinTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.video = self.root / '中文视频.mp4'
        self.video.write_bytes(b'video-fixture')

    def state(self, mode='draft', *extra):
        args = parser_for_cli().parse_args([mode, '--video', str(self.video), '--title', '中文标题', '--description', '第一行\n第二行',
            '--state-dir', str(self.root), '--step-timeout', '2', '--upload-timeout', '3', '--poll-interval', '0.1', *extra])
        state = prepare(args)
        state.save()
        return state

    def workflow(self, state):
        browser = Browser(state)
        clock = Clock()
        return DouyinWorkflow(browser, state, quiet=True, sleep=clock.sleep, clock=clock), browser

    def assert_code(self, code, fn):
        with self.assertRaises(AutomationError) as caught: fn()
        self.assertEqual(caught.exception.code, code)
        return caught.exception

    def test_complete_draft_reload_persisted_fields(self):
        s = self.state('draft', '--visibility', 'private')
        w, b = self.workflow(s)
        w.run()
        self.assertEqual(s.data['phase'], 'draft_saved')
        self.assertEqual(s.data['status'], 'completed')
        self.assertEqual(b.saved['description'], '第一行\n第二行')
        self.assertEqual(b.saved['visibility'], '1')
        self.assertEqual(sum(a == 'click' and v['target'] == 'draft' for a, v in b.calls), 1)
        self.assertTrue(b.path.endswith('/content/upload'))

    def test_draft_waits_for_debounced_application_update(self):
        s = self.state(); w, b = self.workflow(s)
        action = b.action
        times = {}
        def delayed_action(tab, name, **args):
            if name == 'fill' and args.get('target') == 'description':
                times['filled'] = w.clock()
            if name == 'click' and args.get('target') == 'draft':
                times['saved'] = w.clock()
                result = action(tab, name, **args)
                if times['saved'] - times['filled'] < .3:
                    b.saved['description'] = 'old application state'
                return result
            return action(tab, name, **args)
        b.action = delayed_action
        w.run()
        self.assertGreaterEqual(times['saved'] - times['filled'], 1)
        self.assertEqual(s.data['phase'], 'draft_saved')
        self.assertTrue(any(a == 'press' and v.get('key') == 'Tab' for a, v in b.calls))

    def test_stable_poll_restarts_after_false_and_failed_read(self):
        s = self.state(); w, b = self.workflow(s)
        samples = iter([True, False, True, TransportError('temporary'), True, True, True])
        def probe():
            value = next(samples)
            if isinstance(value, Exception):
                raise value
            return {'ready': value}
        w.probe = probe
        result = w.poll(lambda p: p['ready'], 2, 'TIMEOUT', 'fixture', stable_for=.19)
        self.assertTrue(result['ready'])
        self.assertAlmostEqual(w.clock(), .6)

    def test_upload_never_submits(self):
        s = self.state('upload'); w, b = self.workflow(s); w.run()
        self.assertEqual(s.data['phase'], 'editor_ready')
        self.assertFalse(any(a == 'click' and v['target'] in {'draft', 'post'} for a, v in b.calls))

    def test_controlled_schedule_replaces_selection_without_backspace(self):
        s=self.state('post'); date=(datetime.now(ZONE)+timedelta(days=1)).replace(hour=19,minute=30,second=0,microsecond=0)
        s.data['config']['schedule_at']=date.isoformat();w,b=self.workflow(s)
        original_evaluate=b.evaluate;original_action=b.action
        control={'checked':False,'value':'old','selected':False};events=[]
        def evaluate(tab,script):
            if script=='navigator.platform':return 'MacIntel'
            p=original_evaluate(tab,script);p.update(scheduleChecked=control['checked'],scheduleValue=control['value'])
            p['actions'].update(schedule={'enabled':True,'target':'schedule'},scheduleInput={'enabled':True,'target':'date'})
            return p
        def action(tab,name,**args):
            if name=='click' and args.get('target')=='schedule':control['checked']=True
            return original_action(tab,name,**args)
        def cdp(tab,method,params):
            events.append((method,params))
            if params.get('commands')==['selectAll']:control['selected']=True
            if method=='Input.insertText':
                self.assertTrue(control['selected']);control['value']=params['text']
            return {}
        b.evaluate=evaluate;b.action=action;b.cdp=cdp;w.set_schedule()
        self.assertEqual(control['value'],date.strftime('%Y-%m-%d %H:%M'))
        self.assertFalse(any(p.get('key')=='Backspace' for m,p in events))
        self.assertFalse(any(a=='fill' for a,p in b.calls))

    def test_publish_verified_by_create_id(self):
        s = self.state('post'); w, b = self.workflow(s); w.run()
        self.assertEqual(s.data['phase'], 'published')
        self.assertEqual(s.data['post_id'], '1234567890123456789')
        self.assertEqual(sum(a == 'click' and v['target'] == 'post' for a, v in b.calls), 1)

    def test_unknown_submit_never_repeated_on_resume(self):
        s = self.state('post'); w, b = self.workflow(s)
        b.submit_error = TransportError('lost reply')
        self.assert_code('ACTION_OUTCOME_UNKNOWN', w.run)
        self.assertIsNotNone(s.data['submit_intent'])
        before = len(b.calls)
        w.run()
        self.assertEqual(s.data['phase'], 'published')
        self.assertFalse(any(a == 'click' for a, v in b.calls[before:]))

    def test_refused_submit_withdraws_intent(self):
        s = self.state('post'); w, b = self.workflow(s)
        b.submit_error = APIError(422, 'BROWSER_ACTION_FAILED', 'ELEMENT_DISABLED')
        self.assert_code('ACTION_NOT_PERFORMED', w.run)
        self.assertIsNone(s.data['submit_intent'])

    def test_post_missing_id_not_success(self):
        s = self.state('post'); w, b = self.workflow(s)
        b.cdp = lambda *a: {'body': '{"status_code":0}'}
        self.assert_code('POST_UNVERIFIED', w.run)
        self.assertNotEqual(s.data.get('status'), 'completed')

    def test_post_server_rejection(self):
        s = self.state('post'); w, b = self.workflow(s)
        b.cdp = lambda *a: {'body': '{"status_code":8,"status_msg":"作品不符合要求"}'}
        self.assert_code('POST_REJECTED', w.run)

    def test_guard_login_captcha_dialog_error_and_origin(self):
        w, b = self.workflow(self.state())
        for p, code in [({'loginRequired': True}, 'LOGIN_REQUIRED'), ({'challenge': True}, 'VERIFICATION_REQUIRED'),
                        ({'dialog': '请确认'}, 'DIALOG_REQUIRES_ATTENTION'), ({'host': 'evil.invalid'}, 'UNEXPECTED_ORIGIN'),
                        ({'rejected': True, 'alerts': ['格式错误']}, 'SITE_REJECTED')]:
            self.assert_code(code, lambda: w.guard(p))

    def test_old_extension_stops_before_navigation(self):
        w, b = self.workflow(self.state()); b.rev = 9
        self.assert_code('EXTENSION_TOO_OLD', w.run)
        self.assertEqual(b.path, 'about:blank')

    def test_wrong_account_stops_before_upload(self):
        w, b = self.workflow(self.state('draft', '--account', 'other'))
        self.assert_code('ACCOUNT_MISMATCH', w.run)
        self.assertFalse(any(a == 'upload_files' for a, v in b.calls))

    def test_existing_unrelated_draft_preserved(self):
        w, b = self.workflow(self.state())
        b.saved = {'file': {'name': 'other.mp4'}, 'vid': 'other'}
        self.assert_code('EXISTING_DRAFT', w.run)
        self.assertFalse(any(a in {'upload_files', 'click'} for a, v in b.calls))

    def test_changed_video_refused(self):
        w, b = self.workflow(self.state()); self.video.write_bytes(b'changed')
        self.assert_code('VIDEO_CHANGED', w.run)

    def test_lost_upload_does_not_select_again(self):
        s = self.state(); s.save(upload_selected=True)
        w, b = self.workflow(s)
        self.assert_code('UPLOAD_PAGE_LOST', w.run)

    def test_saved_draft_mismatch_never_reports_success(self):
        s = self.state(); w, b = self.workflow(s)
        original = w.verify_saved_draft
        def verify():
            b.saved['description'] += '*'
            original()
        w.verify_saved_draft = verify
        self.assert_code('DRAFT_CONTENT_MISMATCH', w.run)
        self.assertIsNotNone(s.data['submit_intent'])

    def test_files_and_argument_limits(self):
        title = self.root / 'title.txt'; title.write_text('文件标题', encoding='utf-8')
        args = parser_for_cli().parse_args(['draft', '--video', str(self.video), '--title-file', str(title)])
        self.assertEqual(prepare(args).data['config']['title'], '文件标题')
        self.assert_code('INVALID_TITLE', lambda: self.state('draft', '--title', '长' * 31))
        self.assert_code('INVALID_DESCRIPTION', lambda: self.state('draft', '--description', '长' * 1001))
        for value in ['nan', 'inf', '0', '-1']:
            with self.assertRaises(SystemExit), patch('sys.stderr'):
                parser_for_cli().parse_args(['draft', '--video', str(self.video), '--poll-interval', value])

    def test_state_validation_and_file_missing(self):
        s = self.state(); self.assertEqual(load_state(s.path).data['job_id'], s.data['job_id'])
        s.data['config']['upload_timeout'] = 'bad'; s.save()
        self.assert_code('INVALID_STATE', lambda: load_state(s.path))
        self.assert_code('INVALID_STATE', lambda: load_state(self.root / 'missing'))

    def test_events_query_uses_receive_time_and_repeated_filters(self):
        c = BrowserClient()
        with patch.object(c, 'request', return_value={'events': []}) as request:
            self.assertEqual(c.events(12, since=123, methods=['Network.', 'Page.']), [])
            self.assertEqual(request.call_args.args[0], '/api/events?tabId=12&since=123&method=Network.&method=Page.')

    def test_unavailable_api_structured_exit(self):
        with patch('cli.douyin.main.DEFAULT_STATE_DIR', self.root / 'locks'), \
                patch('cli.douyin.main.BrowserClient') as client, patch('builtins.print') as output:
            client.return_value.capabilities.side_effect = TransportError('offline')
            result = main(['draft', '--video', str(self.video), '--state-dir', str(self.root), '--json'])
            self.assertEqual(result, 3)
            data = json.loads(output.call_args.args[0])
            self.assertEqual(data['code'], 'API_UNAVAILABLE')
            self.assertIn('resume_command', data)


if __name__ == '__main__':
    unittest.main()
