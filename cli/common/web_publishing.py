"""Shared lifecycle for browser-only publishing; no platform API or credentials."""
import argparse
import contextlib
import hashlib
import json
import os
from pathlib import Path
import re
import shlex
import sys
import time
import uuid
from urllib.parse import urlparse

from .automation import AutomationError, FileLock, TaskState, claim_key
from .browser_client import APIError, BASE, BrowserClient, TransportError, load_probe
from .media import fingerprint, read_text
from .page_workflow import PageWorkflow, MemoryState, same_text


def state_directory(platform):
    return Path(os.environ.get('XDG_STATE_HOME', str(Path.home()/'.local/state')))/'browser-agent'/platform


def positive(value):
    try:
        result=float(value)
        if 0<result<float('inf'):return result
    except ValueError:
        pass
    raise argparse.ArgumentTypeError('Expected positive finite seconds')


def local_image(path, *, limit=20*1024**2):
    if Path(path).suffix.lower() not in {'.png','.jpg','.jpeg','.gif','.webp'}:
        raise AutomationError('INVALID_IMAGE','Use PNG, JPEG, GIF, or WebP images',exit_code=2)
    result=fingerprint(path,quiet=True,code='INVALID_IMAGE')
    if result['size']>limit:raise AutomationError('INVALID_IMAGE','Image exceeds the supported size limit',exit_code=2)
    return result


def unchanged(item):
    current=fingerprint(item['path'],quiet=True,code='MEDIA_CHANGED')
    if current['sha256']!=item['sha256']:
        raise AutomationError('MEDIA_CHANGED','An input image has changed since task creation',exit_code=2)


def safe_url(value, hosts):
    parsed=urlparse(value)
    if parsed.scheme!='https' or parsed.hostname not in hosts or parsed.username or parsed.password:
        raise AutomationError('INVALID_URL','Expected an HTTPS URL on the target platform',exit_code=2)
    return value


def parser_for(platform, *, article=False):
    parser=argparse.ArgumentParser(description=f'Publish to {platform} through your signed-in Chrome browser; no platform API required.')
    sub=parser.add_subparsers(dest='command',required=True)
    for command in ['account','recent','compose','draft','post','status','resume']:
        q=sub.add_parser(command)
        q.add_argument('--json',action='store_true',help='Output single-line JSON')
        if command=='status':
            q.add_argument('--state-file',type=Path,required=True)
            continue
        q.add_argument('--api-url','--base',default=None,help=f'Browser Agent Proxy URL; default: {BASE}')
        q.add_argument('--quiet',action='store_true')
        if command=='resume':
            q.add_argument('--state-file',type=Path,required=True)
            q.add_argument('--publish',action='store_true',help='Publish this job after a verified compose or saved draft')
            continue
        q.add_argument('--account',help='Expected account identity; stop on mismatch')
        q.add_argument('--step-timeout',type=positive,default=60)
        q.add_argument('--poll-interval',type=positive,default=1)
        if command=='recent':
            q.add_argument('--limit',type=int,default=20)
            continue
        if command=='account':continue
        q.add_argument('--state-dir',type=Path,default=state_directory(platform))
        q.add_argument('--state-file',type=Path)
        q.add_argument('--allow-duplicate',action='store_true')
        q.add_argument('--keep-tab',action='store_true')
        if article:
            q.add_argument('--markdown',type=Path,required=True,help='UTF-8 Markdown file; image paths are relative to this file')
            q.add_argument('--title',help='Override the first Markdown heading as the article title')
        else:
            group=q.add_mutually_exclusive_group()
            group.add_argument('--text')
            group.add_argument('--text-file')
            q.add_argument('--image',action='append',default=[],help='Local image path; repeat for multiple images')
            q.add_argument('--images',nargs='+',default=[],help='Local image paths')
            if platform=='facebook':q.add_argument('--page-url',help='Page URL to open; the active publishing identity must match --account')
    return parser


def validate_config(data):
    c=data['config']
    if c.get('mode') not in {'compose','draft','post'} or not isinstance(c.get('text'),str) or not isinstance(c.get('images'),list) or not isinstance(c.get('state_dir'),str):
        raise AutomationError('INVALID_STATE','Invalid publishing configuration',exit_code=2)
    if data.get('platform')!='medium' and c['mode'] in {'draft','post'} and not c['text'].strip():
        raise AutomationError('INVALID_STATE','A caption is required to verify saved or published social content',exit_code=2)
    for image in c['images']:
        if not isinstance(image,dict) or not isinstance(image.get('path'),str) or not re.fullmatch('[a-f0-9]{64}',str(image.get('sha256'))):
            raise AutomationError('INVALID_STATE','Invalid image fingerprint',exit_code=2)
    if c.get('account') is not None and not isinstance(c['account'],str):
        raise AutomationError('INVALID_STATE','Invalid expected account',exit_code=2)
    if c.get('page_url'):
        safe_url(c['page_url'],{'facebook.com','www.facebook.com'})
        if not c.get('account'):raise AutomationError('INVALID_STATE','A page job requires an expected publishing account',exit_code=2)
    if data.get('platform')=='medium':
        kinds={'paragraph','heading','subheading','quote','code','bullet','number','image'}
        if not isinstance(c.get('title'),str) or not c['title'] or not isinstance(c.get('blocks'),list) or not c['blocks']:
            raise AutomationError('INVALID_STATE','Invalid article title or blocks',exit_code=2)
        for block in c['blocks']:
            if not isinstance(block,dict) or block.get('kind') not in kinds:
                raise AutomationError('INVALID_STATE','Invalid article block',exit_code=2)
            if block['kind']=='image':
                if block.get('image') not in c['images'] or not isinstance(block.get('alt',''),str) or not isinstance(block.get('caption',''),str):
                    raise AutomationError('INVALID_STATE','Invalid article image block',exit_code=2)
            elif not isinstance(block.get('text'),str):
                raise AutomationError('INVALID_STATE','Invalid article text block',exit_code=2)



def prepare_social(args,platform):
    text=read_text(args.text,args.text_file).replace('\r\n','\n').rstrip('\n')
    paths=args.image+args.images
    limit=3000 if platform=='linkedin' else 63206
    if '\x00' in text or len(text)>limit or (not text.strip() and not paths):
        raise AutomationError('INVALID_TEXT',f'Provide text (at most {limit} characters) or images; NUL is not allowed',exit_code=2)
    if args.command in {'post','draft'} and not text.strip():
        raise AutomationError('TEXT_REQUIRED','Include a caption so saved or published content can be verified; compose supports image-only previews',exit_code=2)
    if len(paths)>20:raise AutomationError('INVALID_IMAGE','At most 20 images per job',exit_code=2)
    images=[local_image(path) for path in paths]
    page=getattr(args,'page_url',None)
    if page:
        safe_url(page,{'facebook.com','www.facebook.com'})
        if not args.account:raise AutomationError('ACCOUNT_REQUIRED','--page-url requires --account to verify the publishing identity',exit_code=2)
    return make_state(args,platform,{'text':text,'images':images,'page_url':page})


def make_state(args,platform,content):
    root=args.state_dir.expanduser().resolve();job=uuid.uuid4().hex
    path=args.state_file.expanduser().resolve() if args.state_file else root/job/'state.json'
    if path.exists():raise AutomationError('STATE_EXISTS','State file exists; use resume or a new path',exit_code=2)
    config={'mode':args.command,'account':args.account,'state_dir':str(root),
            'step_timeout':args.step_timeout,'poll_interval':args.poll_interval,
            'allow_duplicate':args.allow_duplicate,'keep_tab':args.keep_tab,**content}
    return TaskState(path,{'schema_version':1,'platform':platform,'job_id':job,'phase':'prepared',
                           'status':'prepared','api_url':args.api_url or BASE,'config':config})


class PublishingWorkflow(PageWorkflow):
    platform=''
    hosts=set()
    home=''
    def __init__(self,client,state,**kwargs):
        super().__init__(client,state,load_probe(Path(__file__).parents[1]/self.platform/'probe.js'),**kwargs)

    def guard(self,p):
        if p.get('challenge'):raise AutomationError('VERIFICATION_REQUIRED','Complete the security check in the retained tab, then resume',exit_code=3)
        if p.get('loginRequired'):raise AutomationError('LOGIN_REQUIRED','Sign in in the retained tab, then resume',exit_code=3)
        if p.get('host') and p['host'] not in self.hosts:raise AutomationError('UNEXPECTED_ORIGIN','The tab left the target platform',exit_code=3)
        if p.get('rejected'):raise AutomationError('SITE_REJECTED','The platform rejected the operation',exit_code=6,details={'alerts':p.get('alerts',[])})
        expected=self.state.data.get('account') or self.config.get('account')
        if p.get('editor') and expected and p.get('account') and p['account'].lstrip('@').casefold()!=expected.lstrip('@').casefold():
            raise AutomationError('ACCOUNT_MISMATCH','The editor publishing identity changed',exit_code=3)
        name=self.state.data.get('account_name')
        if self.platform=='linkedin' and p.get('editor') and name and p.get('actor') and p['actor']!=name:
            raise AutomationError('ACCOUNT_MISMATCH','The editor is using a different publishing identity',exit_code=3)


    def attach(self, *, navigate=True):
        opened=self.ensure_tab(platform=self.platform,min_revision=10,required_actions={'fill','insert_text','press','upload_files','click','set_tab_policy'})
        if navigate:self.navigate(self.config.get('page_url') or self.home)
        p=self.poll(lambda p:p.get('account'),self.config['step_timeout'],'ACCOUNT_UNVERIFIED','Could not identify the signed-in account')
        expected=self.config.get('account') or self.state.data.get('account')
        if expected and expected.lstrip('@').casefold()!=p['account'].lstrip('@').casefold():
            raise AutomationError('ACCOUNT_MISMATCH','The active account differs from the expected account',exit_code=3)
        self.state.save(account=p['account'],profile_url=p.get('profileUrl') or self.state.data.get('profile_url'),account_name=p.get('accountName') or self.state.data.get('account_name'))
        return opened,p

    def recent(self,limit=20):
        profile=self.state.data.get('profile_url')
        if not profile:raise AutomationError('PROFILE_UNVERIFIED','Cannot determine the account content-list URL',exit_code=7)
        self.navigate(self.recent_url(profile))
        self.poll(lambda p:p.get('listReady'),self.config['step_timeout'],'LIST_UNAVAILABLE','Content list did not load')
        seen={};stale=0
        for _ in range(12):
            p=self.probe();before=len(seen)
            for row in p.get('posts',[]):
                if row.get('url'):seen.setdefault(row['url'],row)
            if len(seen)>=limit:break
            stale=stale+1 if len(seen)==before else 0
            if stale>=3:break
            self.mutate('scroll_list','cdp.send',method='Input.dispatchMouseEvent',params={'type':'mouseWheel','x':500,'y':500,'deltaX':0,'deltaY':700})
            self.sleep(self.config['poll_interval'])
        return list(seen.values())[:limit]

    def recent_url(self,profile):return profile

    def verify_editor(self,p):
        return p.get('editor') and same_text(p.get('text'),self.config['text']) and p.get('mediaCount',0)==len(self.config['images']) and not p.get('uploading')

    def compose(self):
        self.open_editor()
        p=self.poll(lambda p:p.get('editor'),self.config['step_timeout'],'EDITOR_UNAVAILABLE','The publishing editor did not open',stable_for=1)
        if p.get('text','').strip() or p.get('mediaCount',0):
            raise AutomationError('EXISTING_DRAFT','An existing editor draft was preserved; resolve it before creating another task',exit_code=7)
        if p.get('account') and p['account']!=self.state.data.get('account'):
            raise AutomationError('ACCOUNT_MISMATCH','The editor publishing identity differs from the verified account',exit_code=3)
        if self.platform=='linkedin' and p.get('actor') and self.state.data.get('account_name') and p['actor']!=self.state.data['account_name']:
            raise AutomationError('ACCOUNT_MISMATCH','The editor is using a different publishing identity; company Page posting is not supported',exit_code=3)
        self.state.save(editing_started=True,editor_url=p['url'])
        self.mutate('focus_editor','click',target=p['actions']['text']['target'])
        p=self.poll(lambda p:p.get('editor'),self.config['step_timeout'],'EDITOR_UNAVAILABLE','Editor did not finish initializing after focus',stable_for=1)
        self.mutate('fill_text','fill',target=p['actions']['text']['target'],value=self.config['text'])
        self.attach_images()
        self.poll(self.verify_editor,self.config['step_timeout'],'EDITOR_MISMATCH','Text or attachments did not match the requested content',stable_for=1)

    def attach_images(self):
        images=self.config['images']
        if not images:return
        for item in images:unchanged(item)
        self.open_media()
        p=self.poll(lambda p:p.get('actions',{}).get('file'),self.config['step_timeout'],'UPLOAD_UNAVAILABLE','Image file input is unavailable')
        self.state.save(media_intent=True)
        self.mutate('upload_images','upload_files',target=p['actions']['file']['target'],files=[i['path'] for i in images])
        self.finish_media()

    def open_media(self):pass
    def finish_media(self):pass

    def submit(self):
        p=self.poll(self.verify_editor,self.config['step_timeout'],'EDITOR_MISMATCH','Editor does not match the requested content')
        # Save intent before the click. A missing reply must never cause a second click.
        self.state.save(submit_intent={'at':time.time()},phase='submit')
        try:self.click('post')
        except AutomationError as error:
            if error.code=='ACTION_NOT_PERFORMED':self.state.save(submit_intent=None)
            raise
        self.poll(lambda p:not p.get('editor'),self.config['step_timeout'],'POST_UNVERIFIED','The editor did not close after submission; inspect the tab before resuming')
        self.verify_publication()

    def verify_publication(self):
        self.sleep(self.config['poll_interval'])
        # Read the account's own list, never the home feed or a toast alone.
        matches=[row for row in self.recent(30) if same_text(row.get('text'),self.config['text'])
                 and row.get('images',0)>=len(self.config['images'])
                 and row.get('url') not in self.state.data.get('baseline_urls',[])]
        if len(matches)!=1:raise AutomationError('POST_UNVERIFIED','No unique new matching work was found; inspect the retained tab. Submission will not be repeated',exit_code=7)
        row=matches[0]
        self.state.save(status='completed',phase='published',post_url=row['url'],evidence={'kind':'own_content_list','item':row})

    def run(self):
        data=self.state.data
        if data.get('submit_intent'):
            self.reconciling=True
            self.attach();self.verify_publication();return
        if data.get('draft_intent') and self.config['mode']=='draft':
            self.restore_draft()
            self.poll(self.verify_editor,self.config['step_timeout'],'DRAFT_UNVERIFIED','The restored draft does not match the job')
            self.state.save(status='completed',phase='draft_saved',evidence={'kind':'restored_draft','persisted':True})
            return
        if data.get('editing_started'):
            opened,p=self.attach(navigate=False)
            if opened:
                raise AutomationError('EDITOR_LOST','The original editor tab is gone; inspect the saved draft before creating another job',exit_code=7)
            if not self.verify_editor(p):
                raise AutomationError('EDITOR_REQUIRES_REVIEW','The retained editor is incomplete or changed; no input will be replayed automatically',exit_code=7)
        else:
            self.attach()
            baseline=self.recent(30)
            if not self.config.get('allow_duplicate') and self.config['text'].strip() and any(same_text(row.get('text'),self.config['text']) for row in baseline):
                raise AutomationError('DUPLICATE_POST','A recent work already has this text',exit_code=7)
            self.state.save(baseline_urls=[r['url'] for r in baseline])
            content=json.dumps({'text':self.config['text'],'images':[i['sha256'] for i in self.config['images']]},sort_keys=True)
            key=hashlib.sha256((data['account']+'\0'+content).encode()).hexdigest()
            claim_key(self.state,Path(self.config['state_dir']),key,allow_duplicate=self.config.get('allow_duplicate',False))
            self.compose()
        if self.config['mode']=='post':self.submit()
        elif self.config['mode']=='draft':self.save_draft()
        else:self.state.save(status='completed',phase='editor_ready',evidence={'kind':'editor_verified','persisted':False})


def result_for(state,error=None):
    data=state.data if state else {}
    result={'success':error is None,'status':data.get('status'),'stage':data.get('phase'),
            'state_file':str(state.path) if state else None,'tab_id':data.get('tab_id'),
            'post_url':data.get('post_url'),'draft_url':data.get('draft_url'),'evidence':data.get('evidence')}
    if state:result['resume_command']=shlex.join([sys.executable,'-m','cli.'+data['platform'],'resume','--state-file',str(state.path)])
    if error:result.update(code=error.code,message=str(error),details=error.details)
    return result


def cli_main(platform,workflow_class,argv=None,*,prepare=prepare_social,article=False):
    parser=parser_for(platform,article=article);args=parser.parse_args(argv)
    if getattr(args,'limit',20)<1 or getattr(args,'limit',20)>200:parser.error('--limit must be between 1 and 200')
    state=workflow=None;writable=False;code=0
    try:
        if args.command in {'status','resume'}:
            state=TaskState.load(args.state_file,platform=platform,validate=validate_config)
            if args.command=='status':
                print(json.dumps(result_for(state),ensure_ascii=False,indent=None if args.json else 2));return 0
        elif args.command in {'account','recent'}:
            state=MemoryState({'job_id':uuid.uuid4().hex,'config':{'account':args.account,'step_timeout':args.step_timeout,'poll_interval':args.poll_interval}})
            workflow=workflow_class(BrowserClient(args.api_url or BASE),state,quiet=args.quiet)
            try:
                workflow.attach()
                result={'success':True,'account':state.data['account'],'profile_url':state.data.get('profile_url')}
                if args.command=='recent':result.update(posts=workflow.recent(args.limit),complete=False)
            finally:
                if workflow.tab_id:
                    with contextlib.suppress(Exception):workflow.client.close_tab(workflow.tab_id)
            print(json.dumps(result,ensure_ascii=False,indent=None if args.json else 2));return 0
        else:state=prepare(args,platform)
        with FileLock(state_directory(platform)/'profile.lock'),FileLock(str(state.path)+'.lock'):
            if args.command=='resume':
                state=TaskState.load(args.state_file,platform=platform,validate=validate_config)
                if args.publish:
                    if state.data.get('submit_intent') or state.data.get('phase')=='published':
                        raise AutomationError('ALREADY_SUBMITTED','A submitted task cannot start a new publication',exit_code=7)
                    if state.data.get('phase') not in {'editor_ready','draft_saved'}:
                        raise AutomationError('INVALID_STAGE','--publish requires a verified editor or saved draft',exit_code=2)
                    # Saved drafts need platform-specific reopening; never create a new upload.
                    state.data['config']['mode']='post'
                    state.data['status']='running'
            if state.data.get('status')!='completed':
                writable=True;state.save(status='running',error=None,api_url=args.api_url or state.data['api_url'])
                workflow=workflow_class(BrowserClient(args.api_url or state.data['api_url']),state,quiet=args.quiet)
                if state.data.get('phase')=='draft_saved' and args.command=='resume' and args.publish:
                    workflow.restore_draft()
                workflow.run()
                if state.data.get('phase')=='published' and not state.data['config'].get('keep_tab'):
                    with contextlib.suppress(Exception):workflow.client.close_tab(workflow.tab_id)
            result=result_for(state)
    except (Exception,KeyboardInterrupt) as caught:
        if isinstance(state,MemoryState):state=None
        if isinstance(caught,AutomationError):error=caught
        elif isinstance(caught,KeyboardInterrupt):error=AutomationError('INTERRUPTED','Interrupted; inspect the retained tab and resume',exit_code=130)
        elif isinstance(caught,(APIError,TransportError)):error=AutomationError('API_UNAVAILABLE',str(caught),exit_code=7 if state and state.data.get('submit_intent') else 3)
        elif isinstance(caught,(OSError,ValueError)):error=AutomationError('LOCAL_IO_ERROR',str(caught),exit_code=2)
        else:error=AutomationError('UNEXPECTED_ERROR',f'{type(caught).__name__}: {caught}',exit_code=4)
        code=error.exit_code
        if writable and state:
            with contextlib.suppress(Exception):state.save(status='failed',error={'code':error.code,'message':str(error)})
            if workflow:
                with contextlib.suppress(Exception):workflow.diagnostics()
        result=result_for(state,error)
    print(json.dumps(result,ensure_ascii=False,indent=None if args.json else 2));return code
