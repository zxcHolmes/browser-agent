"""Offline tests for the TikTok CLI: no browser, the client is mocked."""
from pathlib import Path
import tempfile
import unittest
from unittest.mock import Mock

from cli.common.automation import AutomationError
from cli.common.browser_client import APIError
from cli.tiktok.main import (TikTokWorkflow, build_caption, caption_segments, load_state, parser_for_cli, post_time,
                             prepare_post)


def form(**overrides):
    base = {'caption': {'target': '#cap', 'enabled': True, 'value': '', 'tags': []},
            'status': {'uploaded': True, 'failed': False, 'percent': 100},
            'visibility': {'target': '#vis', 'open': False, 'text': 'Everyone', 'options': []},
            'advanced': {'collapsed': True, 'toggle': {'target': '#more'}},
            'aigc': None, 'suggestions': None,
            'post': {'target': '#post', 'enabled': True, 'loading': False},
            'draft': {'target': '#draft', 'enabled': True, 'loading': False}}
    base.update(overrides)
    return base


def page(**overrides):
    base = {'host': 'www.tiktok.com', 'path': '/tiktokstudio/upload', 'loggedIn': True, 'handle': 'me', 'tour': False,
            'modals': [], 'upload': form(), 'content': None, 'posts': [], 'draftList': None, 'drafts': [], 'alerts': []}
    base.update(overrides)
    return base


def content(posts, **overrides):
    return page(path='/tiktokstudio/content', upload=None, content={'ready': True, 'empty': not posts}, posts=posts, **overrides)


def row(video_id, text, author='me'):
    return {'id': str(video_id), 'author': author, 'kind': 'video', 'text': text, 'privacy': 'Everyone', 'row': text}


class Fixture(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.video = self.root / 'clip.mp4'
        self.video.write_bytes(b'x' * 100)

    def args(self, *extra):
        return parser_for_cli().parse_args(['post', '--video', str(self.video), '--state-dir', str(self.root), '--quiet', *extra])

    def workflow(self, *extra):
        state = prepare_post(self.args(*extra))
        state.save(account='me')
        w = TikTokWorkflow(Mock(), state, quiet=True)
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

    def actions(self, w, name=None):
        return [c for c in w.client.action.call_args_list if name is None or c.args[1] == name]


class CaptionTests(Fixture):
    def test_hashtags_are_appended_and_validated(self):
        self.assertEqual(build_caption('Hello', ['MoneyBrain', '#Psychology']), 'Hello\n\n#MoneyBrain #Psychology')
        self.assertEqual(build_caption('', ['a']), '#a')
        self.assert_code('INVALID_HASHTAG', lambda: build_caption('x', ['two words']))

    def test_segments_keep_order(self):
        self.assertEqual(caption_segments('Hi #a and #b.'), [('text', 'Hi '), ('tag', '#a'), ('text', ' and '), ('tag', '#b'), ('text', '.')])
        self.assertEqual(caption_segments('#only'), [('tag', '#only')])

    def test_id_carries_time(self):
        self.assertEqual(post_time('7696459921243622676'), 1791971717)
        self.assertIsNone(post_time('x'))

    def test_arguments(self):
        self.assert_code('INVALID_VIDEO', lambda: prepare_post(parser_for_cli().parse_args(
            ['post', '--video', str(self.root / 'missing.mp4'), '--state-dir', str(self.root)])))
        bad = self.root / 'clip.txt'
        bad.write_text('x')
        self.assert_code('INVALID_VIDEO', lambda: prepare_post(parser_for_cli().parse_args(['post', '--video', str(bad)])))
        self.assert_code('CAPTION_TOO_LONG', lambda: prepare_post(self.args('--caption', 'x' * 4001)))
        self.assert_code('INVALID_ACCOUNT', lambda: prepare_post(self.args('--account', 'no spaces')))
        state = prepare_post(self.args('--caption', 'Hi', '--hashtag', 'a', '--draft', '--visibility', 'private'))
        self.assertEqual(state.data['config']['caption'], 'Hi\n\n#a')
        self.assertTrue(state.data['config']['draft'])
        self.assertFalse(state.data['config']['ai_generated'])
        state.save()
        self.assertEqual(load_state(state.path).data['config']['visibility'], 'private')


class GuardTests(Fixture):
    def test_stop_conditions(self):
        w = self.workflow()
        self.assert_code('LOGIN_REQUIRED', lambda: w.guard(page(loginRequired=True, loggedIn=False)))
        self.assert_code('VERIFICATION_REQUIRED', lambda: w.guard(page(challenge=True)))
        self.assert_code('UNEXPECTED_ORIGIN', lambda: w.guard(page(host='accounts.google.com')))

    def test_account_mismatch(self):
        w = self.workflow('--account', 'other')
        w.ensure_tab = lambda **kw: False
        w.client.evaluate.return_value = content([row(1, 'x')])
        self.assert_code('ACCOUNT_MISMATCH', w.attach)


class CaptionWritingTests(Fixture):
    def test_hashtag_is_picked_from_the_suggestions(self):
        w = self.workflow('--caption', 'Hi', '--hashtag', 'Tag')
        typed = page(upload=form(caption={'target': '#cap', 'enabled': True, 'value': 'Hi\n\n#Tag', 'tags': []},
                                 suggestions={'items': ['#Tag', '#Tags'], 'focused': 0}))
        picked = page(upload=form(caption={'target': '#cap', 'enabled': True, 'value': 'Hi\n\n#Tag ', 'tags': ['#Tag']}))
        before = page(upload=form(caption={'target': '#cap', 'enabled': True, 'value': 'Hi\n\n', 'tags': []}))
        pages = iter([before, before, typed, picked])
        w.client.evaluate.side_effect = lambda *a: next(pages, picked)
        w.write_caption()
        calls = [(c.args[1], c.kwargs.get('value') or c.kwargs.get('key')) for c in self.actions(w)]
        self.assertEqual(calls, [('fill', 'Hi\n\n'), ('insert_text', '#Tag'), ('press', 'Enter')])

    def test_tag_not_offered_stays_plain(self):
        w = self.workflow('--caption', 'Hi', '--hashtag', 'Tag')
        stuck = page(upload=form(caption={'target': '#cap', 'enabled': True, 'value': 'Hi\n\n#Tag', 'tags': []},
                                 suggestions={'items': ['#Other'], 'focused': 0}))
        closed = page(upload=form(caption={'target': '#cap', 'enabled': True, 'value': 'Hi\n\n#Tag', 'tags': []}))
        state = {'closed': False}

        def evaluate(*a):
            return closed if state['closed'] else stuck

        def action(tab, name, **kw):
            if kw.get('key') == 'Escape':
                state['closed'] = True
            return {}
        w.client.evaluate.side_effect = evaluate
        w.client.action.side_effect = action
        w.write_caption()
        self.assertEqual(w.state.data['plain_hashtags'], ['#Tag'])
        self.assertFalse(self.actions(w, 'press') and any(c.kwargs.get('key') == 'Enter' for c in self.actions(w, 'press')))

    def test_caption_mismatch_is_reported(self):
        w = self.workflow('--caption', 'Hello')
        w.client.evaluate.return_value = page(upload=form(caption={'target': '#cap', 'enabled': True, 'value': 'Hel', 'tags': []}))
        self.assert_code('CAPTION_VERIFICATION_FAILED', w.verify_caption)


class SettingsTests(Fixture):
    def test_visibility_is_picked_by_value(self):
        w = self.workflow('--visibility', 'private')
        options = [{'value': '0', 'selected': True, 'target': '#o0', 'text': 'Everyone'},
                   {'value': '1', 'selected': False, 'target': '#o1', 'text': 'Only you'}]
        pages = iter([page(), page(upload=form(visibility={'target': '#vis', 'open': True, 'text': 'Everyone', 'options': options}))])
        done = page(upload=form(visibility={'target': '#vis', 'open': False, 'text': 'Only you', 'options': []}))
        w.client.evaluate.side_effect = lambda *a: next(pages, done)
        w.set_visibility()
        self.assertEqual([c.kwargs['target'] for c in self.actions(w, 'click')], ['#vis', '#o1'])

    def test_ai_label_off_by_default_turns_a_preset_label_off(self):
        w = self.workflow()
        on = page(upload=form(advanced={'collapsed': False, 'toggle': {'target': '#more'}},
                              aigc={'checked': True, 'disabled': False, 'toggle': {'target': '#ai'}}))
        off = page(upload=form(advanced={'collapsed': False, 'toggle': {'target': '#more'}},
                               aigc={'checked': False, 'disabled': False, 'toggle': {'target': '#ai'}}))
        pages = iter([on])
        w.client.evaluate.side_effect = lambda *a: next(pages, off)
        w.set_ai_label()
        self.assertEqual([c.kwargs['target'] for c in self.actions(w, 'click')], ['#ai'])

    def test_ai_label_on_confirms_the_explanation(self):
        w = self.workflow('--ai-generated')
        collapsed = page()
        shown = page(upload=form(advanced={'collapsed': False, 'toggle': {'target': '#more'}},
                                 aigc={'checked': False, 'disabled': False, 'toggle': {'target': '#ai'}}))
        asking = page(modals=[{'text': 'Label AI content', 'primary': {'target': '#yes'}, 'neutral': {'target': '#no'}}], upload=shown['upload'])
        done = page(upload=form(advanced={'collapsed': False, 'toggle': {'target': '#more'}},
                                aigc={'checked': True, 'disabled': False, 'toggle': {'target': '#ai'}}))
        pages = iter([collapsed, shown, asking])
        w.client.evaluate.side_effect = lambda *a: next(pages, done)
        w.set_ai_label()
        self.assertEqual([c.kwargs['target'] for c in self.actions(w, 'click')], ['#more', '#ai', '#yes'])


class PublishTests(Fixture):
    def published(self, w, caption):
        return content([row(((int(w.state.data['post_intent_at']) + 5) << 32), caption)])

    def test_post_pressed_once_confirming_the_check_dialog(self):
        w = self.workflow('--caption', 'Hello world')
        question = page(modals=[{'text': 'Post anyway?', 'primary': {'target': '#now'}, 'neutral': {'target': '#cancel'}}])
        pages = iter([page(), question, question])

        def evaluate(*a):
            p = next(pages, None)
            return p if p is not None else self.published(w, 'Hello world')
        w.client.evaluate.side_effect = evaluate
        w.publish()
        self.assertEqual([c.kwargs['target'] for c in self.actions(w, 'click')], ['#post', '#now'])
        self.assertEqual(w.state.data['status'], 'completed')
        self.assertTrue(w.state.data['post_id'])

    def test_draft_mode_presses_save_draft_and_checks_drafts(self):
        w = self.workflow('--caption', 'Hello world', '--draft')
        drafts = page(path='/tiktokstudio/content', upload=None, draftList={'count': 1, 'ready': True, 'empty': False},
                      drafts=[{'text': 'Hello world', 'row': 'Hello world'}])
        pages = iter([page()])
        w.client.evaluate.side_effect = lambda *a: next(pages, drafts)
        w.publish()
        self.assertEqual([c.kwargs['target'] for c in self.actions(w, 'click')], ['#draft'])
        self.assertEqual(w.state.data['phase'], 'drafted')

    def test_message_dialog_withdraws_the_intent(self):
        w = self.workflow('--caption', 'Hello')
        pages = iter([page(), page(modals=[{'text': 'Something went wrong', 'primary': {'target': '#ok'}, 'neutral': None}])])
        w.client.evaluate.side_effect = lambda *a: next(pages)
        error = self.assert_code('POST_REJECTED', w.publish)
        self.assertIn('went wrong', str(error))
        self.assertFalse(w.state.data['post_intent'])

    def test_refused_click_withdraws_the_intent(self):
        w = self.workflow('--caption', 'Hello')
        w.client.evaluate.return_value = page()
        w.client.action.side_effect = APIError(422, 'BROWSER_ACTION_FAILED', 'Error: ELEMENT_DISABLED')
        self.assert_code('ACTION_NOT_PERFORMED', w.publish)
        self.assertFalse(w.state.data['post_intent'])

    def test_no_result_is_unknown_and_not_retried(self):
        w = self.workflow('--caption', 'Hello')
        w.client.evaluate.return_value = page()
        self.assert_code('POST_OUTCOME_UNKNOWN', w.publish)
        self.assertEqual(len(self.actions(w, 'click')), 1)
        self.assertTrue(w.state.data['post_intent'])

    def test_resume_after_intent_only_reconciles(self):
        w = self.workflow('--caption', 'Hello')
        w.state.save(post_intent=True, post_intent_at=1000)
        w.attach = lambda: 'me'
        w.client.evaluate.return_value = content([row(1010 << 32, 'Hello')])
        w.run()
        self.assertEqual(w.state.data['status'], 'completed')
        self.assertFalse(self.actions(w, 'click') + self.actions(w, 'upload_files'))

    def test_duplicate_caption_in_posts_or_drafts_stops_before_upload(self):
        w = self.workflow('--caption', 'Hello  WORLD')
        w.attach = lambda: 'me'
        drafts = page(path='/tiktokstudio/content', upload=None, draftList={'count': 1, 'ready': True, 'empty': False},
                      drafts=[{'text': 'hello world', 'row': ''}])

        def evaluate(tab, script):
            return content([row(1, 'Something else')]) if 'tab=draft' not in str(w.client.navigate.call_args) else drafts
        w.client.evaluate.side_effect = evaluate
        error = self.assert_code('DUPLICATE_POST', w.run)
        self.assertEqual(len(error.details['drafts']), 1)
        self.assertFalse(self.actions(w, 'upload_files'))


class SharedWorkflowTests(Fixture):
    def test_transient_refusal_is_retried(self):
        w = self.workflow()
        w.client.action.side_effect = [APIError(422, 'BROWSER_ACTION_FAILED', 'Error: ELEMENT_OBSCURED'), {'clicked': True}]
        self.assertEqual(w.mutate('x', 'click', target='#a'), {'clicked': True})
        self.assertEqual(w.client.action.call_count, 2)

    def test_lasting_refusal_is_not_performed(self):
        w = self.workflow()
        w.client.action.side_effect = APIError(422, 'BROWSER_ACTION_FAILED', 'Error: ELEMENT_NOT_FOUND')
        self.assert_code('ACTION_NOT_PERFORMED', lambda: w.mutate('x', 'click', target='#a'))
        self.assertEqual(w.client.action.call_count, 1)

    def test_navigation_failure_is_retried_then_reported(self):
        w = self.workflow()
        w.client.navigate.side_effect = [APIError(502, 'NAVIGATION_FAILED', 'net::ERR_TUNNEL_CONNECTION_FAILED'), {'ok': True}]
        w.navigate('https://example.com')
        w.client.navigate.side_effect = APIError(502, 'NAVIGATION_FAILED', 'net::ERR_TUNNEL_CONNECTION_FAILED')
        self.assert_code('NAVIGATION_FAILED', lambda: w.navigate('https://example.com'))


if __name__ == '__main__':
    unittest.main()
