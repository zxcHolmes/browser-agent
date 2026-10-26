"""Facebook text/image posts through the signed-in website, without a platform API."""
from ..common.automation import AutomationError
from ..common.web_publishing import PublishingWorkflow,cli_main


class FacebookWorkflow(PublishingWorkflow):
    platform='facebook'
    hosts={'www.facebook.com','facebook.com'}
    home='https://www.facebook.com/'
    drafts_url='https://www.facebook.com/professional_dashboard/content/content_library/?filter=DRAFT'

    def open_editor(self):
        self.navigate(self.config.get('page_url') or self.home)
        self.click('start')

    def draft_match(self,p):
        text=' '.join(self.config['text'].split())
        return [r for r in p.get('draftRows',[]) if text and text in r['text']]

    def find_draft(self):
        deadline=self.clock()+self.config['step_timeout']
        while self.clock()<deadline:
            self.navigate(self.drafts_url)
            p=self.poll(lambda p:p.get('listReady'),min(10,self.config['step_timeout']),'LIST_UNAVAILABLE','Draft content library did not load')
            rows=self.draft_match(p)
            if len(rows)>1:raise AutomationError('DRAFT_AMBIGUOUS','Multiple drafts match this text; inspect them before continuing',exit_code=7)
            if len(rows)==1:return rows[0]
            # The content library can lag a completed save; refresh only this read.
            self.sleep(min(2,max(0,deadline-self.clock())))
        raise AutomationError('DRAFT_UNVERIFIED','The content library has not indexed a unique matching draft; resume to check again',exit_code=7)

    def save_draft(self):
        p=self.probe()
        if not p.get('actions',{}).get('saveDraft'):
            raise AutomationError('DRAFT_UNAVAILABLE','This account does not expose a persistent web draft control; use compose to retain the editor',exit_code=4)
        self.state.save(draft_intent=True)
        self.click('saveDraft')
        self.poll(lambda p:not p.get('editor') and not any('正在保存' in a or 'Saving' in a for a in p.get('alerts',[])),self.config['step_timeout'],'DRAFT_UNVERIFIED','Draft saving did not finish',stable_for=1)
        row=self.find_draft()
        self.mutate('reopen_draft','click',target=row['target']['target'])
        self.poll(self.verify_editor,self.config['step_timeout'],'DRAFT_UNVERIFIED','Reopened draft does not match the text and attachments',stable_for=1)
        self.state.save(status='completed',phase='draft_saved',draft_url=self.probe()['url'],
                        evidence={'kind':'reopened_content_library_draft','persisted':True,'images':len(self.config['images'])})

    def restore_draft(self):
        self.attach()
        row=self.find_draft()
        self.mutate('reopen_draft','click',target=row['target']['target'])
        self.poll(self.verify_editor,self.config['step_timeout'],'DRAFT_UNVERIFIED','Saved draft changed or is unavailable')


def main(argv=None):return cli_main('facebook',FacebookWorkflow,argv)

if __name__=='__main__':raise SystemExit(main())
