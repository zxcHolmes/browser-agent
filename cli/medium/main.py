"""Compose Markdown articles and local images through Medium's website."""
import contextlib
import re
import sys
import time
from pathlib import Path
from ..common.automation import AutomationError
from ..common.web_publishing import PublishingWorkflow,cli_main,make_state,unchanged,safe_url
from .markdown import parse_markdown,inline_parts,plain_text


def prepare(args,platform='medium'):
    content=parse_markdown(args.markdown,args.title)
    for block in content['blocks']:
        if block['kind'] not in {'image','code'}:inline_parts(block['text'])
    return make_state(args,platform,content)


class MediumWorkflow(PublishingWorkflow):
    platform='medium'
    hosts={'medium.com','www.medium.com'}
    home='https://medium.com/me'
    modifier='Meta' if sys.platform=='darwin' else 'Control'

    def attach(self, *, navigate=True):
        opened=self.ensure_tab(platform=self.platform,min_revision=11,required_actions={'insert_text','press','click','upload_files','set_tab_policy'})
        if navigate:self.navigate(self.home)
        elif opened and self.state.data.get('editing_started'):
            saved=self.state.data.get('draft_url') or self.state.data.get('editor_url','')
            if not re.search(r'/p/[a-z0-9]+/edit',saved):raise AutomationError('EDITOR_LOST','The article tab is gone and no saved draft URL is available',exit_code=7)
            self.navigate(safe_url(saved,self.hosts))
        if not navigate and not self.probe().get('account'):
            p=self.probe()
            if p.get('actions',{}).get('userMenu'):self.click('userMenu')
        p=self.poll(lambda p:p.get('account'),self.config['step_timeout'],'ACCOUNT_UNVERIFIED','Could not identify the signed-in Medium account')
        expected=self.config.get('account') or self.state.data.get('account')
        if expected and expected.lstrip('@').casefold()!=p['account'].lstrip('@').casefold():
            raise AutomationError('ACCOUNT_MISMATCH','The signed-in Medium account differs from the expected account',exit_code=3)
        self.state.save(account=p['account'],profile_url=p['profileUrl'])
        if not navigate:self.mutate('close_account_menu','press',key='Escape')
        return False,p

    def recent_url(self,profile):return 'https://medium.com/me/stories/public'

    def key(self,key):self.mutate('key_'+key,'press',key=key)

    def type(self,text,target='.postArticle-content[g_editable="true"]'):
        if not text:return
        # Medium clears placeholder spans on keypress, which Input.insertText
        # alone does not emit. Start with one trusted character, then insert
        # the remaining text at the same caret through the semantic action.
        first=text[0]
        params={'key':first,'text':first}
        if first=='`':params.update(code='Backquote',windowsVirtualKeyCode=192)
        if first.isascii() and first.isalnum():
            params.update(code=('Digit' if first.isdigit() else 'Key')+first.upper(),windowsVirtualKeyCode=ord(first.upper()))
        self.mutate('type_first_character','cdp.send',method='Input.dispatchKeyEvent',params={'type':'keyDown',**params})
        self.mutate('release_first_character','cdp.send',method='Input.dispatchKeyEvent',params={'type':'keyUp',**{k:v for k,v in params.items() if k!='text'}})
        remainder=text[1:]
        core=remainder.rstrip(' ')
        if core:self.mutate('type_article_text','insert_text',target=target,value=core,position='caret')
        # Medium temporarily represents trailing spaces with NBSP; send them as
        # keystrokes and verify normalized text once the whole block is complete.
        for _ in range(len(remainder)-len(core)):
            self.mutate('type_space','cdp.send',method='Input.dispatchKeyEvent',params={'type':'keyDown','key':' ','code':'Space','windowsVirtualKeyCode':32,'text':' '})
            self.mutate('release_space','cdp.send',method='Input.dispatchKeyEvent',params={'type':'keyUp','key':' ','code':'Space','windowsVirtualKeyCode':32})

    def inline(self,text):
        for part in inline_parts(text):
            kind=part['kind']
            if kind in {'bold','italic'}:
                key=self.modifier+'+'+('b' if kind=='bold' else 'i')
                self.key(key);self.type(part['text']);self.key(key)
            elif kind=='code':
                self.type('`');self.type(part['text']);self.type('`')
            elif kind=='link':
                # Preserve both label and destination without depending on the
                # editor's transient hyperlink popup. Linked labels are not
                # supported by this importer; the URL remains visible.
                self.type(part['text']+' ('+part['url']+')')
            else:self.type(part['text'])

    def insert_image(self,block):
        unchanged(block['image'])
        self.sleep(1)
        before=self.probe()['mediaCount']
        self.client.cdp(self.tab_id,'Page.setInterceptFileChooserDialog',{'enabled':True})
        try:
            since=int(time.time()*1000)
            for attempt in range(3):
                if not self.probe().get('imageMenuOpen'):self.click('add')
                try:
                    self.poll(lambda p:p.get('imageMenuOpen') and p.get('actions',{}).get('image'),3,'IMAGE_MENU_UNAVAILABLE','Image menu did not open',stable_for=.3)
                    break
                except AutomationError as error:
                    if error.code!='IMAGE_MENU_UNAVAILABLE' or attempt==2:raise
                    # Opening the menu is reversible; retry only after observing it closed.
                    if self.probe().get('imageMenuOpen'):raise
            self.click('image')
            deadline=self.clock()+self.config['step_timeout'];node=None
            while self.clock()<deadline:
                events=self.client.events(self.tab_id,since=since,methods=['Page.fileChooserOpened'])
                for event in events:
                    if event.get('eventMethod')=='Page.fileChooserOpened':node=event.get('eventParams',{}).get('backendNodeId')
                if node:break
                self.sleep(self.config['poll_interval'])
            if not node:raise AutomationError('UPLOAD_UNAVAILABLE','No browser file chooser was observed',exit_code=5)
            self.state.save(media_intent=True)
            self.mutate('upload_article_image','cdp.send',method='DOM.setFileInputFiles',params={'backendNodeId':node,'files':[block['image']['path']]})
        finally:
            with contextlib.suppress(Exception):self.client.cdp(self.tab_id,'Page.setInterceptFileChooserDialog',{'enabled':False})
        self.poll(lambda p:p['mediaCount']==before+1 and not p['uploading'],self.config['step_timeout'],'UPLOAD_UNVERIFIED','Article image did not finish uploading')
        if block.get('caption'):
            self.mutate('select_caption_image','click',target='.postArticle-content figure:last-of-type img')
            self.mutate('focus_caption','click',target='.postArticle-content figure:last-of-type figcaption')
            self.type(block['caption'],target='.postArticle-content figure:last-of-type figcaption')
            self.poll(lambda p:p['blocks'][-1].get('caption')==block['caption'],self.config['step_timeout'],'CAPTION_MISMATCH','Image caption did not settle',stable_for=1)
        # The editor creates a trailing empty paragraph after each uploaded figure.
        self.mutate('focus_after_image','click',target='[g_editable="true"] .section-inner:last-child > p:last-child')

    def compose(self):
        self.navigate('https://medium.com/new-story')
        self.poll(lambda p:p.get('editor') and p.get('actions',{}).get('title'),self.config['step_timeout'],'EDITOR_UNAVAILABLE','Medium editor did not load',stable_for=1)
        p=self.probe()
        if p['blocks'] or p['title']:raise AutomationError('EXISTING_DRAFT','An existing article was preserved',exit_code=7)
        self.state.save(editing_started=True,editor_url=p['url'])
        self.mutate('focus_title','click',target=p['actions']['title']['target'])
        self.type(self.config['title'])
        self.poll(lambda p:p['title']==self.config['title'],self.config['step_timeout'],'TITLE_MISMATCH','Article title did not settle',stable_for=1)
        self.key('Enter')
        self.poll(lambda p:bool(re.search(r'/p/[a-z0-9]+/edit',p.get('url',''))),self.config['step_timeout'],'DRAFT_UNAVAILABLE','Medium has not assigned a stable draft URL',stable_for=1)
        for index,block in enumerate(self.config['blocks']):
            kind=block['kind']
            if kind=='image':self.insert_image(block)
            else:
                shortcut={'heading':'1','subheading':'2','quote':'5','code':'6'}.get(kind)
                if shortcut:self.key(self.modifier+'+Alt+'+shortcut)
                if kind in {'bullet','number'}:
                    self.type('*' if kind=='bullet' else '1.');self.key('Space')
                if kind=='code':
                    for n,line in enumerate(block['text'].split('\n')):
                        if n:self.key('Shift+Enter')
                        self.type(line)
                else:self.inline(block['text'])
                self.key('Enter')
                if kind in {'quote','code','bullet','number'}:self.key('Enter')
            self.state.save(blocks_written=index+1,editor_url=self.probe()['url'])
        self.poll(self.verify_editor,self.config['step_timeout'],'EDITOR_MISMATCH','Article blocks do not match Markdown',stable_for=1)
        self.state.save(draft_url=self.probe()['url'])

    def verify_editor(self,p):
        if not p.get('editor') or p.get('title')!=self.config['title'] or p.get('uploading'):return False
        observed=p.get('blocks',[]);expected=self.config['blocks']
        if len(observed)!=len(expected):return False
        for actual,wanted in zip(observed,expected):
            if actual['kind']!=wanted['kind']:return False
            if wanted['kind']=='image':
                if not actual.get('loaded') or actual.get('caption','')!=wanted.get('caption',''):return False
            elif actual.get('text')!=plain_text(wanted):return False
            elif wanted['kind']!='code':
                parts=inline_parts(wanted['text'])
                expected_formats=[p for p in parts if p['kind'] not in {'text','link'}]
                actual_formats=[p for p in actual.get('formats',[]) if not (p['kind']=='link' and p['text'].rstrip('/')==p.get('url','').rstrip('/'))]
                if actual_formats!=expected_formats:return False
        return True

    def save_draft(self):
        # A quiet interval plus a reload verifies the server-saved editor state.
        p=self.poll(self.verify_editor,self.config['step_timeout'],'DRAFT_UNVERIFIED','Article has not settled',stable_for=3)
        url=safe_url(p['url'],self.hosts)
        if not re.search(r'/p/[a-z0-9]+/edit',url):raise AutomationError('DRAFT_UNVERIFIED','Medium has not assigned a draft URL',exit_code=7)
        self.navigate(url)
        self.poll(self.verify_editor,self.config['step_timeout'],'DRAFT_UNVERIFIED','Reloaded article differs from the requested content',stable_for=1)
        self.state.save(status='completed',phase='draft_saved',draft_url=url,editor_url=url,
                        evidence={'kind':'reloaded_article','persisted':True,'images':len(self.config['images'])})

    def restore_draft(self):
        self.attach();url=safe_url(self.state.data['draft_url'],self.hosts)
        self.navigate(url)
        self.poll(self.verify_editor,self.config['step_timeout'],'DRAFT_UNVERIFIED','Saved article changed or is unavailable')

    def submit(self):
        self.save_draft()
        self.state.save(status='running',phase='prepublish')
        self.click('prepublish')
        p=self.poll(lambda p:p.get('prepublish'),self.config['step_timeout'],'PUBLISH_DIALOG_UNAVAILABLE','Medium publication dialog did not open')
        if p.get('previewTitle') is not None and p['previewTitle']!=self.config['title']:
            raise AutomationError('PREVIEW_MISMATCH','The publication preview title differs from the article title',exit_code=6)
        self.state.save(submit_intent={'at':time.time()},phase='submit')
        try:self.click('post')
        except AutomationError as error:
            if error.code=='ACTION_NOT_PERFORMED':self.state.save(submit_intent=None)
            raise
        self.verify_publication()

    def verify_publication(self):
        if getattr(self,'reconciling',False):
            match=re.search(r'/p/([a-z0-9]+)/edit',self.state.data.get('editor_url',''))
            if not match:raise AutomationError('POST_UNVERIFIED','The original article ID is unavailable; inspect Medium manually',exit_code=7)
            self.navigate('https://medium.com/p/'+match[1])
        def matched(p):
            return not p.get('editor') and p.get('articleTitle')==self.config['title'] and p.get('articleImages',0)>=len(self.config['images'])
        p=self.poll(matched,self.config['step_timeout'],'POST_UNVERIFIED','Published article could not be verified; publication will not be repeated')
        expected=[plain_text(b) for b in self.config['blocks'] if b['kind']!='image']
        actual='\n'.join(p.get('articleText',[]))
        if not all(t in actual for t in expected):raise AutomationError('POST_UNVERIFIED','Published body does not match the requested article',exit_code=7)
        self.state.save(status='completed',phase='published',post_url=p['url'],evidence={'kind':'published_article','images':p['articleImages']})


def main(argv=None):return cli_main('medium',MediumWorkflow,argv,prepare=prepare,article=True)

if __name__=='__main__':raise SystemExit(main())
