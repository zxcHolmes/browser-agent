"""Synthetic regression cases for shared publishing safety and LinkedIn flows."""
import contextlib
import io
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch
from cli.common.automation import AutomationError,TaskState
from cli.common.browser_client import TransportError,APIError
from cli.common.page_workflow import MemoryState
from cli.common.web_publishing import parser_for,prepare_social,cli_main
from cli.linkedin.main import LinkedInWorkflow


class LinkedInTests(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory();self.addCleanup(self.temp.cleanup)
        self.root=Path(self.temp.name)
        self.config={'mode':'post','text':'Synthetic post','images':[],'account':None,
                     'state_dir':str(self.root),'step_timeout':.01,'poll_interval':.001}
        self.state=TaskState(self.root/'state.json',{'schema_version':1,'platform':'linkedin','job_id':'fixture-job',
                           'phase':'prepared','status':'running','config':self.config,'account':'example-member'})
        self.workflow=LinkedInWorkflow(None,self.state,sleep=lambda _:None)

    def test_input_limits_and_mutually_exclusive_text(self):
        parser=parser_for('linkedin')
        for options in [[],['--text','x'*3001],['--text','bad\x00text']]:
            args=parser.parse_args(['post','--state-dir',str(self.root),*options])
            with self.assertRaises(AutomationError):prepare_social(args,'linkedin')
        with contextlib.redirect_stderr(io.StringIO()),self.assertRaises(SystemExit):
            parser.parse_args(['post','--text','one','--text-file','two.txt'])

    def test_media_are_fingerprinted_and_missing_files_fail_before_browser(self):
        image=self.root/'image.png';image.write_bytes(b'synthetic image')
        args=parser_for('linkedin').parse_args(['compose','--image',str(image),'--state-dir',str(self.root)])
        state=prepare_social(args,'linkedin')
        self.assertEqual(len(state.data['config']['images']),1)
        self.assertFalse(state.path.exists())
        image.unlink()
        with self.assertRaises(AutomationError):prepare_social(args,'linkedin')

    def test_lost_submit_reply_is_not_replayed(self):
        p={'editor':True,'text':self.config['text'],'mediaCount':0}
        self.workflow.client=type('Client',(),{'action':lambda *a,**k:(_ for _ in ()).throw(TransportError('lost response'))})()
        with patch.object(self.workflow,'poll',return_value=p),patch.object(self.workflow,'click',side_effect=AutomationError('ACTION_OUTCOME_UNKNOWN','lost',exit_code=7)):
            with self.assertRaises(AutomationError):self.workflow.submit()
        self.assertTrue(self.state.data['submit_intent'])
        with patch.object(self.workflow,'attach'),patch.object(self.workflow,'verify_publication') as verify,patch.object(self.workflow,'compose') as compose:
            self.workflow.run()
        verify.assert_called_once();compose.assert_not_called()

    def test_refused_submit_withdraws_intent(self):
        with patch.object(self.workflow,'poll',return_value={}),patch.object(self.workflow,'click',side_effect=AutomationError('ACTION_NOT_PERFORMED','disabled')):
            with self.assertRaises(AutomationError):self.workflow.submit()
        self.assertIsNone(self.state.data['submit_intent'])

    def test_existing_draft_is_never_overwritten(self):
        with patch.object(self.workflow,'open_editor'),patch.object(self.workflow,'poll',return_value={'editor':True,'text':'Existing user draft'}),patch.object(self.workflow,'mutate') as mutate:
            with self.assertRaises(AutomationError) as caught:self.workflow.compose()
        self.assertEqual(caught.exception.code,'EXISTING_DRAFT');mutate.assert_not_called()

    def test_partial_editor_resume_does_not_repeat_input(self):
        self.state.data['editing_started']=True
        with patch.object(self.workflow,'attach',return_value=(False,{'editor':True,'text':'partial','mediaCount':0})),patch.object(self.workflow,'mutate') as mutate:
            with self.assertRaises(AutomationError) as caught:self.workflow.run()
        self.assertEqual(caught.exception.code,'EDITOR_REQUIRES_REVIEW');mutate.assert_not_called()

    def test_baseline_work_cannot_verify_a_new_submission(self):
        self.state.data['baseline_urls']=['https://www.linkedin.com/feed/update/urn:li:activity:100/']
        row={'url':self.state.data['baseline_urls'][0],'text':self.config['text'],'images':0}
        with patch.object(self.workflow,'recent',return_value=[row]),self.assertRaises(AutomationError) as caught:
            self.workflow.verify_publication()
        self.assertEqual(caught.exception.code,'POST_UNVERIFIED')

    def test_account_switch_stops_before_composition(self):
        with patch.object(self.workflow,'ensure_tab',return_value=False),patch.object(self.workflow,'navigate'),patch.object(self.workflow,'poll',return_value={'account':'other-member'}):
            with self.assertRaises(AutomationError) as caught:self.workflow.attach()
        self.assertEqual(caught.exception.code,'ACCOUNT_MISMATCH')

    def test_save_draft_reopens_and_verifies_attachments(self):
        calls=[]
        with patch.object(self.workflow,'click',side_effect=calls.append),patch.object(self.workflow,'poll',return_value={}) as poll,patch.object(self.workflow,'open_editor') as reopen:
            self.workflow.save_draft()
        self.assertEqual(calls,['close','saveDraft']);reopen.assert_called_once()
        self.assertEqual(self.state.data['phase'],'draft_saved')
        self.assertEqual(poll.call_count,3)

    def test_resume_publish_rejects_already_submitted_state(self):
        self.state.save(submit_intent={'at':1},phase='published',status='completed',api_url='http://127.0.0.1:12345')
        with contextlib.redirect_stdout(io.StringIO()) as output:
            code=cli_main('linkedin',LinkedInWorkflow,['resume','--state-file',str(self.state.path),'--publish','--json'])
        self.assertEqual(code,7);self.assertEqual(json.loads(output.getvalue())['code'],'ALREADY_SUBMITTED')

    def test_retained_editor_identity_switch_is_rejected_during_recovery(self):
        self.state.data['account_name']='Example Member'
        with self.assertRaises(AutomationError) as caught:
            self.workflow.guard({'editor':True,'account':'example-member','actor':'Example Company'})
        self.assertEqual(caught.exception.code,'ACCOUNT_MISMATCH')

    def test_uncertain_draft_save_only_reopens_and_checks(self):
        self.state.data['draft_intent']=True;self.config['mode']='draft'
        with patch.object(self.workflow,'restore_draft') as restore,patch.object(self.workflow,'poll',return_value={}),patch.object(self.workflow,'save_draft') as save:
            self.workflow.run()
        restore.assert_called_once();save.assert_not_called()
        self.assertEqual(self.state.data['phase'],'draft_saved')

if __name__=='__main__':unittest.main()
