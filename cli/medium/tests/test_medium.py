"""Medium Markdown and recovery tests; all inputs are synthetic."""
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch
from cli.common.automation import AutomationError
from cli.common.page_workflow import MemoryState
from cli.medium.markdown import parse_markdown,inline_parts,plain_text
from cli.medium.main import MediumWorkflow


class MediumTests(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory();self.addCleanup(self.temp.cleanup)
        self.root=Path(self.temp.name);self.path=self.root/'article.md'
        (self.root/'image.png').write_bytes(b'synthetic image')

    def parse(self,text):
        self.path.write_text(text);return parse_markdown(self.path)

    def test_images_preserve_interleaving_and_resolve_relative_to_markdown(self):
        c=self.parse('# Example article\n\nBefore.\n\n![Alt text](image.png "Caption")\n\nAfter.')
        self.assertEqual(c['title'],'Example article')
        self.assertEqual([b['kind'] for b in c['blocks']],['paragraph','image','paragraph'])
        self.assertEqual(c['images'][0]['path'],str((self.root/'image.png').resolve()))
        self.assertEqual(c['blocks'][1]['caption'],'Caption')

    def test_supported_block_and_inline_syntax(self):
        c=self.parse('# Example\n\n## Heading\n\n### Subheading\n\n> Quote\n\n- Bullet\n\n1. Number\n\n```python\nprint(1)\n```')
        self.assertEqual([b['kind'] for b in c['blocks']],['heading','subheading','quote','bullet','number','code'])
        parts=inline_parts('A **bold** and *italic* [link](https://example.com/) with `code`.')
        self.assertEqual([p['kind'] for p in parts],['text','bold','text','italic','text','link','text','code','text'])
        self.assertEqual(plain_text({'kind':'paragraph','text':'A **bold** word.'}),'A bold word.')
        self.assertEqual(plain_text({'kind':'paragraph','text':'A [link](https://example.com/).'}),'A link (https://example.com/).')

    def test_remote_missing_and_inline_images_are_rejected_before_browser(self):
        for image in ['![alt](https://example.com/image.png)','![alt](missing.png)','Text ![alt](image.png)']:
            with self.assertRaises((AutomationError,OSError)):self.parse('# Example\n\n'+image)

    def test_unsupported_or_invalid_markdown_is_not_silently_flattened(self):
        for body in ['```\nunclosed','| A | B |','<script>bad()</script>','#### Deep heading','  - nested']:
            with self.assertRaises(AutomationError):self.parse('# Example\n\n'+body)
        with self.assertRaises(AutomationError):inline_parts('An **unfinished emphasis')

    def test_block_order_and_loaded_images_are_verified(self):
        c=self.parse('# Example\n\nBefore.\n\n![alt](image.png)\n\nAfter.')
        workflow=MediumWorkflow(None,MemoryState({'config':c}))
        p={'editor':True,'title':'Example','blocks':[{'kind':'paragraph','text':'Before.'},{'kind':'image','loaded':True,'caption':'','alt':'alt'},{'kind':'paragraph','text':'After.'}]}
        self.assertTrue(workflow.verify_editor(p))
        p['blocks'][0],p['blocks'][2]=p['blocks'][2],p['blocks'][0]
        self.assertFalse(workflow.verify_editor(p))
        p['blocks'][0],p['blocks'][2]=p['blocks'][2],p['blocks'][0]
        p['blocks'][1]['loaded']=False
        self.assertFalse(workflow.verify_editor(p))

    def test_saved_draft_requires_reload_verification(self):
        c=self.parse('# Example\n\nBody.');c.update(step_timeout=1,poll_interval=.01)
        state=MemoryState({'config':c});workflow=MediumWorkflow(None,state)
        with patch.object(workflow,'poll',side_effect=[{'url':'https://medium.com/p/example123/edit'},AutomationError('DRAFT_UNVERIFIED','missing body')]),patch.object(workflow,'navigate') as navigate:
            with self.assertRaises(AutomationError):workflow.save_draft()
        navigate.assert_called_once()
        self.assertNotEqual(state.data.get('phase'),'draft_saved')

    def test_title_override_consumes_the_original_first_heading(self):
        self.path.write_text('# Original title\n\nBody.')
        c=parse_markdown(self.path,title='Override title')
        self.assertEqual(c['title'],'Override title')
        self.assertEqual(c['blocks'],[{'kind':'paragraph','text':'Body.'}])

    def test_plain_text_alone_cannot_verify_requested_emphasis(self):
        c=self.parse('# Example\n\nA **bold** word.')
        workflow=MediumWorkflow(None,MemoryState({'config':c}))
        p={'editor':True,'title':'Example','blocks':[{'kind':'paragraph','text':'A bold word.','formats':[]}]}
        self.assertFalse(workflow.verify_editor(p))
        p['blocks'][0]['formats']=[{'kind':'bold','text':'bold'}]
        self.assertTrue(workflow.verify_editor(p))

    def test_uncertain_publication_checks_the_original_article_id(self):
        c=self.parse('# Example\n\nBody.');c.update(step_timeout=1,poll_interval=.01)
        state=MemoryState({'config':c,'editor_url':'https://medium.com/p/example123/edit','submit_intent':{'at':1}})
        workflow=MediumWorkflow(None,state);workflow.reconciling=True
        with patch.object(workflow,'navigate') as navigate,patch.object(workflow,'poll',return_value={'url':'https://medium.com/@example/example123','articleText':['Body.'],'articleImages':0}),patch.object(workflow,'mutate') as mutate:
            workflow.verify_publication()
        navigate.assert_called_once_with('https://medium.com/p/example123');mutate.assert_not_called()
        self.assertEqual(state.data['phase'],'published')

if __name__=='__main__':unittest.main()
