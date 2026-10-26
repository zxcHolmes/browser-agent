"""Facebook adapter validation using synthetic account and post data."""
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch
from cli.common.automation import AutomationError
from cli.common.page_workflow import MemoryState
from cli.common.web_publishing import parser_for,prepare_social,safe_url
from cli.facebook.main import FacebookWorkflow


class FacebookTests(unittest.TestCase):
    def test_page_requires_expected_account_and_platform_url(self):
        with tempfile.TemporaryDirectory() as directory:
            for url,account in [('https://example.com/page','example-page'),('https://www.facebook.com/example-page',None)]:
                options=['post','--text','Synthetic post','--page-url',url,'--state-dir',directory]
                if account:options+=['--account',account]
                with self.assertRaises(AutomationError):prepare_social(parser_for('facebook').parse_args(options),'facebook')

    def test_personal_accounts_without_drafts_get_truthful_error(self):
        state=MemoryState({'config':{'text':'Synthetic post','images':[],'step_timeout':1,'poll_interval':.01}})
        workflow=FacebookWorkflow(None,state)
        with patch.object(workflow,'probe',return_value={'actions':{}}),patch.object(workflow,'click') as click:
            with self.assertRaises(AutomationError) as caught:workflow.save_draft()
        self.assertEqual(caught.exception.code,'DRAFT_UNAVAILABLE');click.assert_not_called()

    def test_draft_selection_requires_unique_matching_content(self):
        state=MemoryState({'config':{'text':'Synthetic\npost','images':[]}})
        workflow=FacebookWorkflow(None,state)
        rows={'draftRows':[{'text':'Other work'},{'text':'Synthetic post Draft'}]}
        self.assertEqual(len(workflow.draft_match(rows)),1)
        rows['draftRows'].append({'text':'Synthetic post Another draft'})
        self.assertEqual(len(workflow.draft_match(rows)),2)

    def test_changed_attachment_is_rejected_before_upload(self):
        with tempfile.TemporaryDirectory() as directory:
            image=Path(directory)/'image.png';image.write_bytes(b'first')
            args=parser_for('facebook').parse_args(['compose','--text','Synthetic post','--image',str(image),'--state-dir',directory])
            state=prepare_social(args,'facebook');image.write_bytes(b'changed')
            workflow=FacebookWorkflow(None,state)
            with patch.object(workflow,'mutate') as mutate,self.assertRaises(AutomationError) as caught:workflow.attach_images()
            self.assertEqual(caught.exception.code,'MEDIA_CHANGED');mutate.assert_not_called()

    def test_external_or_credential_urls_are_rejected(self):
        for url in ['http://www.facebook.com/example','https://www.facebook.com.evil.example/','https://user:pass@www.facebook.com/']:
            with self.assertRaises(AutomationError):safe_url(url,FacebookWorkflow.hosts)

    def test_draft_index_lag_refreshes_reads_without_saving_twice(self):
        state=MemoryState({'config':{'text':'Synthetic post','images':[],'step_timeout':5,'poll_interval':.01}})
        workflow=FacebookWorkflow(None,state,sleep=lambda _:None)
        row={'text':'Synthetic post Draft','target':{'target':'#fixture-draft'}}
        with patch.object(workflow,'navigate') as navigate,patch.object(workflow,'poll',side_effect=[{'draftRows':[]},{'draftRows':[row]}]),patch.object(workflow,'mutate') as mutate:
            self.assertEqual(workflow.find_draft(),row)
        self.assertEqual(navigate.call_count,2);mutate.assert_not_called()

if __name__=='__main__':unittest.main()
