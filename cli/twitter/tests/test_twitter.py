"""Offline tests for the X CLI: no browser, the client is mocked."""
from pathlib import Path
import tempfile
import unittest
from unittest.mock import Mock

from cli.common.automation import AutomationError
from cli.common.browser_client import APIError
from cli.twitter.main import (TwitterWorkflow, classify_media, comparable, load_state, parser_for_cli,
                              prepare_post, same_post_text)


def composer(**overrides):
    base = {'text': {'target': '[role="dialog"] [data-testid="tweetTextarea_0"]', 'enabled': True, 'value': ''},
            'fileInput': {'target': '[role="dialog"] [data-testid="fileInput"]'},
            'media': {'images': 0, 'videos': 0, 'uploading': False},
            'post': {'target': '[data-testid="tweetButton"]', 'enabled': True}, 'alerts': []}
    base.update(overrides)
    return base


def page(**overrides):
    base = {'host': 'x.com', 'path': '/home', 'loggedIn': True, 'handle': 'me', 'toasts': [], 'posts': [],
            'timeline': {'ready': True, 'empty': False}, 'composer': None}
    base.update(overrides)
    return base


class Fixture(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)

    def args(self, *extra):
        return parser_for_cli().parse_args(['post', '--state-dir', str(self.root), '--quiet', *extra])

    def file(self, name, size=10):
        path = self.root / name
        path.write_bytes(b'x' * size)
        return str(path)

    def workflow(self, state):
        w = TwitterWorkflow(Mock(), state, quiet=True)
        now = [0.0]
        w.clock = lambda: now[0]
        w.sleep = lambda n: now.__setitem__(0, now[0] + n)
        w.tab_id = 1
        return w

    def assert_code(self, code, callback):
        with self.assertRaises(AutomationError) as caught:
            callback()
        self.assertEqual(caught.exception.code, code)
        return caught.exception


class TextTests(unittest.TestCase):
    def test_links_case_and_spacing_ignored(self):
        self.assertEqual(comparable('Hello  https://example.com/a?b=1\n World'), 'hello world')
        self.assertTrue(same_post_text('hello   world', 'Hello\nWorld https://t.co/x'))
        self.assertTrue(same_post_text('See example.com/abc… now', 'See https://example.com/abcdef now'))

    def test_truncated_post_matches_as_prefix(self):
        self.assertTrue(same_post_text('A long post that was cut', 'A long post that was cut off by Show more', truncated=True))
        self.assertFalse(same_post_text('A long post that was cut', 'A long post that was cut off by Show more'))

    def test_empty_never_matches(self):
        self.assertFalse(same_post_text('', ''))
        self.assertFalse(same_post_text('https://example.com', 'https://example.com'))


class PrepareTests(Fixture):
    def test_media_rules(self):
        images = [self.file(f'{i}.jpg') for i in range(5)]
        self.assert_code('INVALID_MEDIA', lambda: classify_media(images, None))
        self.assert_code('INVALID_MEDIA', lambda: classify_media(images[:1], self.file('v.mp4')))
        self.assert_code('INVALID_MEDIA', lambda: classify_media([self.file('a.gif'), images[0]], None))
        self.assert_code('INVALID_MEDIA', lambda: classify_media([self.file('a.bmp')], None))
        self.assert_code('INVALID_MEDIA', lambda: classify_media(None, self.file('v.avi')))
        self.assert_code('INVALID_MEDIA', lambda: classify_media([str(self.root / 'missing.jpg')], None))
        kinds = [m['kind'] for m in classify_media(images[:4], None)]
        self.assertEqual(kinds, ['image'] * 4)
        self.assertEqual(classify_media(None, self.file('v.mp4'))[0]['kind'], 'video')

    def test_post_arguments(self):
        self.assert_code('EMPTY_POST', lambda: prepare_post(self.args()))
        self.assert_code('EMPTY_POST', lambda: prepare_post(self.args('--text', '  ')))
        self.assert_code('INVALID_ACCOUNT', lambda: prepare_post(self.args('--text', 'hi', '--account', 'not a handle')))
        state = prepare_post(self.args('--image', self.file('a.png')))
        self.assertEqual(state.data['config']['text'], '')
        text_file = self.root / 'post.txt'
        text_file.write_text('﻿Line 1\nLine 2\n', encoding='utf-8')
        state = prepare_post(self.args('--text-file', str(text_file), '--account', '@Me'))
        self.assertEqual(state.data['config']['text'], 'Line 1\nLine 2')
        self.assertEqual(state.data['config']['account'], 'Me')

    def test_state_round_trip(self):
        state = prepare_post(self.args('--text', 'hello', '--video', self.file('v.mp4')))
        state.save()
        self.assertEqual(load_state(state.path).data['config']['media'][0]['kind'], 'video')


class GuardTests(Fixture):
    def test_stop_conditions(self):
        w = self.workflow(prepare_post(self.args('--text', 'hi')))
        for flag, code in [('loginRequired', 'LOGIN_REQUIRED'), ('challenge', 'VERIFICATION_REQUIRED'),
                           ('accountLocked', 'ACCOUNT_LOCKED')]:
            self.assert_code(code, lambda: w.guard(page(**{flag: True})))
        self.assert_code('UNEXPECTED_ORIGIN', lambda: w.guard(page(host='accounts.google.com')))

    def test_account_mismatch(self):
        state = prepare_post(self.args('--text', 'hi', '--account', 'other'))
        w = self.workflow(state)
        w.ensure_tab = lambda **kw: False
        w.client.evaluate.return_value = page(handle='me')
        self.assert_code('ACCOUNT_MISMATCH', w.attach)


class PublishTests(Fixture):
    def ready_state(self, *extra):
        state = prepare_post(self.args('--text', 'Hello world', *extra))
        state.save(account='me')
        return state

    def test_post_pressed_once_and_verified_by_toast_id(self):
        w = self.workflow(self.ready_state())
        pages = iter([page(composer=composer()),
                      page(toasts=[{'text': 'sent', 'statusId': '42', 'author': 'me'}])])
        timeline = page(path='/me', posts=[{'id': '42', 'author': 'me', 'datetime': '2030-01-01T00:00:00.000Z', 'text': 'Hello world',
                                           'truncated': False, 'images': 0, 'video': False}])
        w.client.evaluate.side_effect = lambda *a: next(pages, timeline)
        w.publish()
        clicks = [c for c in w.client.action.call_args_list if c.args[1] == 'click']
        self.assertEqual(len(clicks), 1)
        self.assertEqual(w.state.data['post_id'], '42')
        self.assertEqual(w.state.data['status'], 'completed')

    def test_rejection_is_reported_with_the_site_message(self):
        w = self.workflow(self.ready_state())
        pages = iter([page(composer=composer()),
                      page(composer=composer(), toasts=[{'text': 'Whoops! You already said that.', 'statusId': None}])])
        w.client.evaluate.side_effect = lambda *a: next(pages)
        error = self.assert_code('POST_REJECTED', w.publish)
        self.assertIn('already said that', str(error))
        self.assertTrue(w.state.data['post_intent'])

    def test_disabled_post_button_is_not_clicked(self):
        w = self.workflow(self.ready_state())
        w.client.evaluate.return_value = page(composer=composer(post={'target': '#p', 'enabled': False}))
        self.assert_code('POST_BLOCKED', w.publish)
        w.client.action.assert_not_called()

    def test_no_result_is_unknown_not_retried(self):
        w = self.workflow(self.ready_state())
        w.client.evaluate.return_value = page(composer=composer())
        self.assert_code('POST_OUTCOME_UNKNOWN', w.publish)
        self.assertEqual(len([c for c in w.client.action.call_args_list if c.args[1] == 'click']), 1)

    def test_refused_click_withdraws_post_intent(self):
        w = self.workflow(self.ready_state())
        w.client.evaluate.return_value = page(composer=composer())
        w.client.action.side_effect = APIError(422, 'BROWSER_ACTION_FAILED', 'Error: ELEMENT_OBSCURED\n    at pageOperation')
        self.assert_code('ACTION_NOT_PERFORMED', w.publish)
        self.assertFalse(w.state.data['post_intent'])
        self.assertEqual(w.state.data['last_action']['outcome'], 'not_performed')

    def test_lost_click_reply_keeps_post_intent(self):
        w = self.workflow(self.ready_state())
        w.client.evaluate.return_value = page(composer=composer())
        w.client.action.side_effect = APIError(422, 'BROWSER_ACTION_FAILED', 'JAVASCRIPT_DIALOG_OPEN: the action may already have executed')
        self.assert_code('ACTION_OUTCOME_UNKNOWN', w.publish)
        self.assertTrue(w.state.data['post_intent'])

    def test_open_autocomplete_is_closed_before_posting(self):
        w = self.workflow(self.ready_state())
        w.client.evaluate.return_value = page(composer=composer(text={'target': '#t', 'value': 'Hello world'}))
        w.close_autocomplete(page(composer=composer(autocomplete=True, text={'target': '#t', 'value': 'Hello world'})))
        w.client.action.assert_called_once_with(1, 'press', target='#t', key='Escape')

    def test_resume_after_post_intent_only_reconciles(self):
        state = self.ready_state()
        state.save(post_intent=True, post_intent_at=0)
        w = self.workflow(state)
        w.attach = lambda: 'me'
        w.client.evaluate.return_value = page(path='/me', posts=[
            {'id': '7', 'author': 'me', 'datetime': '2030-01-01T00:00:00.000Z', 'text': 'Hello world', 'truncated': False,
             'images': 0, 'video': False}])
        w.run()
        self.assertEqual(state.data['post_id'], '7')
        self.assertFalse([c for c in w.client.action.call_args_list if c.args[1] in {'click', 'fill', 'upload_files'}])

    def test_duplicate_text_stops_before_composing(self):
        w = self.workflow(self.ready_state())
        w.attach = lambda: 'me'
        w.client.evaluate.return_value = page(path='/me', posts=[
            {'id': '9', 'author': 'me', 'datetime': '2030-01-01T00:00:00.000Z', 'text': 'hello   WORLD', 'truncated': False,
             'images': 0, 'video': False}])
        error = self.assert_code('DUPLICATE_POST', w.run)
        self.assertEqual(error.details['matches'][0]['id'], '9')
        w.client.action.assert_not_called()

    def test_media_rejection_and_wait(self):
        state = prepare_post(self.args('--image', self.file('a.png')))
        state.save(account='me')
        w = self.workflow(state)
        w.client.action.return_value = {'files': [{'name': 'a.png', 'size': 10}]}
        w.client.evaluate.return_value = page(composer=composer(alerts=['Image too large']))
        self.assert_code('MEDIA_REJECTED', w.attach_media)

        w = self.workflow(state)
        w.client.action.return_value = {'files': [{'name': 'a.png', 'size': 10}]}
        pages = iter([page(composer=composer()),
                      page(composer=composer(media={'images': 1, 'videos': 0, 'uploading': True})),
                      page(composer=composer(media={'images': 1, 'videos': 0, 'uploading': False}))])
        w.client.evaluate.side_effect = lambda *a: next(pages)
        w.attach_media()
        self.assertTrue(state.data['media_attached'])


class RecentTests(Fixture):
    def test_own_posts_only_across_scrolls(self):
        state = prepare_post(self.args('--text', 'hi'))
        state.save(account='me')
        w = self.workflow(state)
        post = lambda i, author='me': {'id': str(i), 'author': author, 'datetime': f'2030-01-0{i}T00:00:00.000Z',
                                        'text': f'post {i}', 'truncated': False, 'images': 0, 'video': False}
        screens = [page(path='/me', posts=[post(1), post(2, 'someone')]),
                   page(path='/me', posts=[post(1), post(2, 'someone')]),
                   page(path='/me', posts=[post(3)])]
        reads = []

        def evaluate(tab, script):
            if script.startswith('window.scrollBy'):
                return True
            reads.append(1)
            return screens[min(len(reads) - 1, len(screens) - 1)]
        w.client.evaluate.side_effect = evaluate
        posts = w.recent_posts(limit=2)
        self.assertEqual([p['id'] for p in posts], ['3', '1'])


if __name__ == '__main__':
    unittest.main()
