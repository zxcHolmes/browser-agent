"""LinkedIn personal posts through the signed-in website, without a platform API."""
from ..common.web_publishing import PublishingWorkflow,cli_main


class LinkedInWorkflow(PublishingWorkflow):
    platform='linkedin'
    hosts={'www.linkedin.com','linkedin.com'}
    home='https://www.linkedin.com/feed/'

    def recent_url(self,profile):return profile.rstrip('/')+'/recent-activity/all/'

    def open_editor(self):
        self.navigate('https://www.linkedin.com/sharing/compose/')

    def open_media(self):
        self.click('media')

    def finish_media(self):
        self.poll(lambda p:p.get('mediaCount',0)>=len(self.config['images']) and not p.get('uploading'),
                  self.config['step_timeout'],'UPLOAD_UNVERIFIED','Image preview did not finish loading')
        self.click('next')
        self.poll(lambda p:p.get('editor'),self.config['step_timeout'],'EDITOR_UNAVAILABLE','Post editor did not return after image confirmation')

    def save_draft(self):
        self.click('close')
        self.poll(lambda p:p.get('draftPrompt'),self.config['step_timeout'],'DRAFT_UNAVAILABLE','Save-draft confirmation did not appear')
        self.state.save(draft_intent=True)
        self.click('saveDraft')
        self.poll(lambda p:not p.get('editor') and not p.get('draftPrompt'),self.config['step_timeout'],'DRAFT_UNVERIFIED','Draft save did not complete')
        self.open_editor()
        self.poll(self.verify_editor,self.config['step_timeout'],'DRAFT_UNVERIFIED','Reopened draft does not match text and attachments',stable_for=1)
        self.state.save(status='completed',phase='draft_saved',draft_url='https://www.linkedin.com/sharing/compose/',
                        evidence={'kind':'reopened_editor','persisted':True,'images':len(self.config['images'])})

    def restore_draft(self):
        self.attach();self.open_editor()
        self.poll(self.verify_editor,self.config['step_timeout'],'DRAFT_UNVERIFIED','Saved draft changed or is unavailable')


def main(argv=None):return cli_main('linkedin',LinkedInWorkflow,argv)

if __name__=='__main__':raise SystemExit(main())
