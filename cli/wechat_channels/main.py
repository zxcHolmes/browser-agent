"""WeChat Channels CLI: list works, upload, save drafts, and publish now or on schedule."""
import argparse
import contextlib
from datetime import datetime
import json
import os
import re
from pathlib import Path
import shlex
import sys
import time
import uuid

from ..common.automation import AutomationError, FileLock, TaskState, claim_key
from ..common.browser_client import APIError, BASE, BrowserClient, TransportError, load_probe
from ..common.media import fingerprint, read_text
from ..common.page_workflow import MemoryState, PageWorkflow, same_text, same_published_text
from ..common.publications import observation_key,listing_result,select_publications
from ..common.scheduling import schedule_time, display_time

SITE='https://channels.weixin.qq.com'
LIST_URL=SITE+'/platform/post/list'
CREATE_URL=SITE+'/platform/post/create'
STATE_DIR=Path(os.environ.get('XDG_STATE_HOME',str(Path.home()/'.local/state')))/'browser-agent/wechat-channels'
PROBE=load_probe(Path(__file__).with_name('probe.js'))


def short_title(value):
    value=value.translate(str.maketrans({'「':'“','」':'”','『':'“','』':'”'}))
    value=' '.join(value.replace('，',' ').replace(',',' ').replace('、',' ').split())
    if re.search(r"""[^\w\s《》「」『』“”‘’"':：+＋?？%％℃]""",value) or '_' in value:
        raise AutomationError('INVALID_TITLE','Short title contains unsupported symbols; commas are replaced with spaces',exit_code=2)
    return value


def positive(value):
    try: n=float(value)
    except ValueError: raise argparse.ArgumentTypeError('must be a positive finite number')
    if not 0<n<float('inf'):raise argparse.ArgumentTypeError('must be a positive finite number')
    return n


def validate(data):
    c=data['config']
    if c.get('mode') not in {'post','draft','upload'} or not isinstance(c.get('video'),dict) or not isinstance(c.get('title'),str) or not isinstance(c.get('description'),str):
        raise AutomationError('INVALID_STATE','Invalid WeChat task fields',exit_code=2)
    if not isinstance(c['video'].get('path'),str) or len(str(c['video'].get('sha256')))!=64 or not 0<len(c['title'])<=16 or len(c['description'])>2000:
        raise AutomationError('INVALID_STATE','Invalid media or text fields',exit_code=2)
    for key in ['upload_timeout','step_timeout','poll_interval']:
        if not isinstance(c.get(key),(int,float)) or not 0<c[key]<float('inf'):raise AutomationError('INVALID_STATE',f'Invalid {key}',exit_code=2)


def load_state(path):return TaskState.load(path,platform='wechat_channels',validate=validate)


def resume_title(state,title=None,title_file=None):
    if title is None and title_file is None:return
    if state.data.get('submit_intent') or state.data.get('direct_post_intent') or state.data.get('status')=='completed':
        raise AutomationError('ALREADY_SUBMITTED','Submitted or completed tasks cannot change title; only verification is allowed',exit_code=7)
    value=short_title(read_text(title,title_file,'').strip())
    if not 6<=len(value)<=16 or any(x in value for x in ['\n','\x00']):
        raise AutomationError('INVALID_TITLE','Short title must contain 6–16 characters on one line',exit_code=2)
    config=dict(state.data['config']);old=config['title'];config['title']=value
    edits=list(state.data.get('config_edits',[]));edits.append({'field':'title','from':old,'to':value,'at':time.time()})
    state.save(config=config,config_edits=edits)


class WeChatWorkflow(PageWorkflow):
    def __init__(self,client,state,**kwargs):super().__init__(client,state,PROBE,**kwargs)

    def guard(self,p):
        if p.get('loginRequired') or re.search(r'/login(?:\.html)?(?:[/?#]|$)',p.get('url','')):raise AutomationError('LOGIN_REQUIRED','Sign in to WeChat Channels in the retained tab, then resume',exit_code=3)
        if p.get('challenge'):raise AutomationError('VERIFICATION_REQUIRED','WeChat Channels requires manual security verification',exit_code=3)
        if p.get('host') and p['host']!='channels.weixin.qq.com':raise AutomationError('UNEXPECTED_ORIGIN','Page left WeChat Channels Assistant',exit_code=3)
        if p.get('rejected'):raise AutomationError('SITE_REJECTED','；'.join(p['alerts']),exit_code=6)
        if p.get('dialogs') and not (self.state.data.get('submit_intent') and p.get('actions',{}).get('directPost')):
            raise AutomationError('DIALOG_REQUIRES_ATTENTION','；'.join(p['dialogs']),exit_code=4)

    def attach(self):
        return self.ensure_tab(platform='wechat_channels',required_actions={'upload_files','fill','press','click','set_tab_policy'},min_revision=10)

    def open(self,url,predicate):
        self.navigate(url)
        return self.poll(predicate,self.config['step_timeout'],'PAGE_NOT_READY_TIMEOUT','WeChat Channels page did not become ready')

    def account(self,p):
        expected=self.config.get('account') or self.state.data.get('account')
        if not p.get('account'):raise AutomationError('ACCOUNT_UNVERIFIED','Cannot read the signed-in channel name',exit_code=3)
        if expected and expected!=p['account']:raise AutomationError('ACCOUNT_MISMATCH',f"Current account: {p['account']}; expected: {expected}",exit_code=3)
        self.state.save(account=p['account'])

    def recent(self,search=None,limit=200):
        self.attach()
        p=self.open(LIST_URL,lambda p:p.get('listReady') and (p.get('rows') or p.get('listEnd') or p.get('total')==0))
        self.account(p)
        if search:
            self.mutate('search_posts','fill',target=p['actions']['search']['target'],value=search)
            self.mutate('search_submit','press',target=p['actions']['search']['target'],key='Enter')
            self.sleep(1)
        rows={};observed=set();complete=False
        for _ in range(100):
            p=self.probe()
            for r in p.get('rows',[]):
                key=observation_key(r);observed.add(key)
                if not search or search in r['text']:rows[key]=r
            total=p.get('total')
            if (p.get('listEnd') and (search or total is None or len(observed)>=total)) or total==0 or (isinstance(total,int) and len(observed)>=total):complete=True;break
            next_page=p.get('actions',{}).get('nextPage')
            if not next_page or not next_page.get('enabled'):break
            if len(rows)>=limit:break
            before=[r['text'] for r in p.get('rows',[])];before_page=p.get('page')
            self.mutate('next_page','click',target=next_page['target'])
            self.poll(lambda p:[r['text'] for r in p.get('rows',[])]!=before and (before_page is None or p.get('page')!=before_page),self.config['step_timeout'],'LIST_INCOMPLETE','Next page did not load',stable_for=.5)
        return listing_result(self.state.data['account'],rows.values(),complete=complete,limit=limit,total=total)

    def fields_match(self,p):return same_text(p.get('title'),self.config['title']) and same_text(p.get('description'),self.config['description'])

    def set_schedule(self):
        wanted=self.config.get('schedule_at');p=self.probe()
        if not wanted:
            if p.get('scheduleChecked'):self.click('immediate')
            return
        date=schedule_time(wanted)
        if not p.get('scheduleChecked'):self.click('schedule')
        self.click('date')
        for _ in range(24):
            p=self.poll(lambda p:p.get('calendar'),self.config['step_timeout'],'CALENDAR_UNAVAILABLE','Calendar did not open')
            month=(p['calendar']['year'],p['calendar']['month'])
            if month==(date.year,date.month):break
            self.click('nextMonth' if month<(date.year,date.month) else 'prevMonth')
        else:raise AutomationError('SCHEDULE_UNAVAILABLE','Date is outside the selectable months',exit_code=6)
        p=self.probe();day=p.get('dayActions',{}).get(str(date.day))
        if not day:raise AutomationError('SCHEDULE_UNAVAILABLE','Requested date is unavailable; it may exceed the platform scheduling window',exit_code=6)
        self.mutate('select_day','click',target=day['target'])
        p=self.poll(lambda p:p.get('actions',{}).get('time'),self.config['step_timeout'],'TIME_UNAVAILABLE','Time input did not appear')
        self.mutate('fill_time','fill',target=p['actions']['time']['target'],value=date.strftime('%H:%M'))
        self.mutate('commit_time','press',target=p['actions']['time']['target'],key='Enter')
        self.mutate('blur_time','click',target=p['actions']['title']['target'])
        self.poll(lambda p:p.get('scheduleChecked') and p.get('scheduleValue')==display_time(wanted),self.config['step_timeout'],'SCHEDULE_MISMATCH','Page schedule differs from the requested time',stable_for=1)

    def verify_result(self):
        mode=self.config['mode']
        if mode=='draft':
            self.open(SITE+'/platform/post/draft',lambda p:'草稿' in p.get('text',''))
            p=self.poll(lambda p:self.config['title'] in p.get('text',''),self.config['step_timeout'],'DRAFT_UNVERIFIED','Matching content was not found in drafts')
            self.state.save(status='completed',phase='draft_saved',evidence={'kind':'draft_list_match','text':p['text'][-1500:]})
            return
        p=self.open(LIST_URL,lambda p:p.get('listReady') and (p.get('rows') or p.get('listEnd')))
        self.account(p)
        wanted=self.config.get('schedule_at')
        def matched(p):
            result=[]
            for r in p.get('rows',[]):
                if not same_published_text(r['text'],self.config['description']):continue
                if wanted:
                    t=datetime.fromisoformat(wanted)
                    dates=[t.strftime('%Y年%m月%d日 %H:%M'),t.strftime('%Y-%m-%d %H:%M')]
                    if not any(s in r['body'] for s in dates) or not any(s in r['body'] for s in ['定时','待发表','待发布','将于']):continue
                result.append(r)
            return result
        p=self.poll(matched,self.config['step_timeout'],'POST_UNVERIFIED','Requested work or schedule is not visible; cannot confirm success or repeat submission')
        rows=matched(p)
        if len(rows)!=1:raise AutomationError('POST_UNVERIFIED','Multiple works have the same title; cannot verify uniquely',exit_code=7)
        self.state.save(status='completed',phase='scheduled' if wanted else 'published',scheduled_at=wanted,result_url=LIST_URL,evidence={'kind':'platform_list_match','item':rows[0],'schedule_at':wanted})

    def run(self):
        self.attach()
        if self.state.data.get('submit_intent'):return self.verify_result()
        p=self.probe()
        if not (p.get('editor') and self.state.data.get('upload_selected')):
            if self.state.data.get('upload_selected'):
                raise AutomationError('UPLOAD_PAGE_LOST','Upload editor is lost; upload will not be repeated automatically',exit_code=7)
            listing=self.recent()
            if not listing['complete']:
                raise AutomationError('LIST_INCOMPLETE','Content list is incomplete; publication stopped to prevent duplicates',exit_code=7)
            if not self.config.get('allow_duplicate') and any(same_published_text(r['text'],self.config['description']) for r in listing['posts']):
                raise AutomationError('DUPLICATE_POST','A work or schedule with the same description already exists',exit_code=7)
            p=self.open(CREATE_URL,lambda p:p.get('editor') and p.get('loggedIn'))
        self.account(p)
        if not self.state.data.get('upload_selected'):
            v=fingerprint(self.config['video']['path'],quiet=True,code='INVALID_VIDEO')
            if v['sha256']!=self.config['video']['sha256']:raise AutomationError('VIDEO_CHANGED','Video file has changed',exit_code=2)
            claim_key(self.state,self.config['state_dir'],v['sha256']+':'+self.state.data['account'],allow_duplicate=self.config.get('allow_duplicate',False))
            self.phase('upload','Upload video and wait for platform processing')
            self.state.save(upload_selected=True)
            try:self.mutate('upload_video','upload_files',target=p['actions']['upload']['target'],files=[v['path']])
            except AutomationError as error:
                if error.code=='ACTION_NOT_PERFORMED':self.state.save(upload_selected=False)
                raise
        self.poll(lambda p:p.get('uploaded'),self.config['upload_timeout'],'UPLOAD_TIMEOUT','Upload or processing did not complete; tab retained')
        self.phase('metadata','Fill title and description')
        for key in ['title','description']:
            p=self.probe()
            if not same_text(p.get(key),self.config[key]):self.mutate('fill_'+key,'fill',target=p['actions'][key]['target'],value=self.config[key])
        self.mutate('blur_description','press',target=self.probe()['actions']['description']['target'],key='Tab')
        self.poll(self.fields_match,self.config['step_timeout'],'TEXT_MISMATCH','Input text was not applied correctly',stable_for=1)
        self.set_schedule()
        if self.config['mode']=='upload':
            self.state.save(status='completed',phase='editor_ready',result_url=CREATE_URL);return
        key='draft' if self.config['mode']=='draft' else 'post'
        def ready(p):
            if p.get('validationErrors'):
                raise AutomationError('INPUT_REJECTED','；'.join(p['validationErrors']),exit_code=6)
            return (p.get('actions',{}).get(key) or {}).get('enabled')
        p=self.poll(ready,self.config['step_timeout'],'CONTROL_UNAVAILABLE','Submit button is unavailable')
        if not self.fields_match(p):raise AutomationError('TEXT_MISMATCH','Text differs before submission',exit_code=6)
        if self.config.get('schedule_at'):
            schedule_time(self.config['schedule_at'])
            if not p.get('scheduleChecked') or p.get('scheduleValue')!=display_time(self.config['schedule_at']):raise AutomationError('SCHEDULE_MISMATCH','Schedule differs before submission',exit_code=6)
        self.state.save(submit_intent={'kind':key,'at':time.time()})
        try:self.mutate('submit_'+key,'click',target=p['actions'][key]['target'])
        except AutomationError as error:
            if error.code=='ACTION_NOT_PERFORMED':self.state.save(submit_intent=None)
            raise
        # WeChat may offer an originality claim; do not claim it automatically.
        deadline=self.clock()+self.config['step_timeout']
        while self.clock()<deadline:
            p=self.probe()
            if p.get('path')=='/platform/post/list':break
            if p.get('dialogs') and (p.get('actions',{}).get('directPost') or {}).get('enabled'):
                if self.state.data.get('direct_post_intent'):raise AutomationError('POST_UNVERIFIED','Publication confirmation outcome is unknown; click will not be repeated',exit_code=7)
                self.state.save(direct_post_intent=True)
                self.mutate('confirm_direct_post','click',target=p['actions']['directPost']['target'])
            self.sleep(self.config['poll_interval'])
        else:raise AutomationError('POST_UNVERIFIED','Editor remains open after submission; inspect platform messages before continuing',exit_code=7)
        self.verify_result()


def parser_for_cli():
    p=argparse.ArgumentParser(prog='cli.wechat_channels',description=__doc__)
    sub=p.add_subparsers(dest='command',required=True)
    def options(q):
        q.add_argument('--api-url','--base',default=None,help=f'Proxy URL; default: {BASE}')
        q.add_argument('--json',action='store_true');q.add_argument('--quiet',action='store_true');q.add_argument('--keep-tab',action='store_true')
    def timing(q):
        q.add_argument('--step-timeout',type=positive,default=90);q.add_argument('--poll-interval',type=positive,default=1)
    for mode in ['post','draft','upload']:
        q=sub.add_parser(mode,help={'post':'Publish immediately or on schedule','draft':'Save a draft','upload':'Upload and retain the editor'}[mode]);options(q);timing(q)
        q.add_argument('--video',required=True,help='Local MP4 path; at most 20 GiB')
        t=q.add_mutually_exclusive_group();t.add_argument('--title',help='Short title of 6–16 characters');t.add_argument('--title-file',help='UTF-8 short-title file')
        t=q.add_mutually_exclusive_group();t.add_argument('--description','--caption',help='Video description; at most 2000 characters');t.add_argument('--description-file','--caption-file',help='UTF-8 video description file')
        q.add_argument('--account',help='Expected WeChat channel name')
        q.add_argument('--allow-duplicate',action='store_true')
        q.add_argument('--upload-timeout',type=positive,default=1800)
        q.add_argument('--state-dir',type=Path,default=STATE_DIR);q.add_argument('--state-file',type=Path)
        if mode=='post':q.add_argument('--schedule-at',help='Shanghai time or an ISO timestamp with timezone')
    q=sub.add_parser('recent',help='Read published and scheduled works');options(q);timing(q)
    q.add_argument('--search');q.add_argument('--account');q.add_argument('--limit',type=int,default=200)
    q.add_argument('--status',choices=['scheduled','published','reviewing','rejected','draft','unknown'],help='Filter by observed platform publication status')
    q.add_argument('--require-complete',action='store_true',help='Return a nonzero exit code if the list is incomplete')
    q=sub.add_parser('resume',help='Resume a task; submitted tasks are verified without replay');options(q);q.add_argument('--state-file',required=True,type=Path)
    t=q.add_mutually_exclusive_group();t.add_argument('--title',help='Correct the short title only before submission');t.add_argument('--title-file',help='UTF-8 corrected short-title file; allowed only before submission')
    q=sub.add_parser('status',help='Read local state only');q.add_argument('--state-file',required=True,type=Path);q.add_argument('--json',action='store_true')
    return p


def prepare(args):
    title=short_title(read_text(args.title,args.title_file,Path(args.video).stem).strip())
    description=read_text(args.description,args.description_file,title).replace('\r\n','\n').rstrip('\n')
    if not 6<=len(title)<=16 or any(x in title for x in ['\n','\x00']):raise AutomationError('INVALID_TITLE','Short title must contain 6–16 characters on one line',exit_code=2)
    if len(description)>2000 or '\x00' in description:raise AutomationError('INVALID_DESCRIPTION','Description must contain at most 2000 characters and no NUL',exit_code=2)
    if Path(args.video).suffix.lower() not in {'.mp4','.mov','.m4v'}:raise AutomationError('INVALID_VIDEO','Use MP4/H.264',exit_code=2)
    video=fingerprint(args.video,quiet=True,code='INVALID_VIDEO')
    if video['size']>20*1024**3:raise AutomationError('INVALID_VIDEO','Video exceeds 20 GiB',exit_code=2)
    scheduled=schedule_time(getattr(args,'schedule_at',None))
    job_id=uuid.uuid4().hex;directory=args.state_dir.expanduser().resolve();path=args.state_file.expanduser().resolve() if args.state_file else directory/job_id/'state.json'
    if path.exists():raise AutomationError('STATE_EXISTS','State file exists; use resume',exit_code=2)
    config={'mode':args.command,'title':title,'description':description,'video':video,'account':args.account,
            'schedule_at':scheduled.isoformat() if scheduled else None,'allow_duplicate':args.allow_duplicate,
            'upload_timeout':args.upload_timeout,'step_timeout':args.step_timeout,'poll_interval':args.poll_interval,'state_dir':str(directory)}
    return TaskState(path,{'schema_version':1,'platform':'wechat_channels','job_id':job_id,'phase':'prepared','status':'prepared','created_at':time.time(),'config':config,'api_url':args.api_url or BASE})


def summarize(state,error=None):
    d=state.data if state else {}
    result={'success':not error and d.get('status')=='completed','status':d.get('status'),'stage':d.get('phase'),'account':d.get('account'),
            'job_id':d.get('job_id'),'tab_id':d.get('tab_id'),'scheduled_at':d.get('scheduled_at'),'url':d.get('result_url'),'state_file':str(state.path) if state else None,'evidence':d.get('evidence')}
    if state:result['resume_command']=shlex.join([sys.executable,str(Path(__file__).resolve().parent),'resume','--state-file',str(state.path)])
    if error:result.update(code=error.code,message=str(error),details=error.details)
    return result


def main(argv=None):
    args=parser_for_cli().parse_args(argv);state=workflow=None;writable=False;code=0
    try:
        if args.command=='recent':
            if not 1<=args.limit<=1000:raise AutomationError('INVALID_ARGUMENT','limit must be 1..1000',exit_code=2)
            memory=MemoryState({'job_id':uuid.uuid4().hex,'config':{'account':args.account,'step_timeout':args.step_timeout,'poll_interval':args.poll_interval}})
            workflow=WeChatWorkflow(BrowserClient(args.api_url or BASE),memory,quiet=args.quiet)
            try:
                result=select_publications(workflow.recent(args.search,1000 if args.status else args.limit),args.status,args.limit)
                if args.require_complete and not result['complete']:raise AutomationError('LIST_INCOMPLETE','Content list is incomplete',exit_code=7,details=result)
            finally:
                if workflow.tab_id:
                    with contextlib.suppress(Exception):workflow.client.close_tab(workflow.tab_id)
        else:
            state=load_state(args.state_file) if args.command in {'status','resume'} else prepare(args)
            if args.command=='status':result=summarize(state)
            else:
                with FileLock(STATE_DIR/'profile.lock'),FileLock(str(state.path)+'.lock'):
                    if args.command=='resume':
                        state=load_state(args.state_file)
                        resume_title(state,args.title,args.title_file)
                    if state.data.get('status')=='completed':result=summarize(state)
                    else:
                        writable=True;state.save(status='running',error=None,api_url=args.api_url or state.data.get('api_url') or BASE)
                        workflow=WeChatWorkflow(BrowserClient(state.data['api_url']),state,quiet=args.quiet);workflow.run();result=summarize(state)
                        if state.data['config']['mode']=='post' and not args.keep_tab:
                            with contextlib.suppress(Exception):workflow.client.close_tab(workflow.tab_id)
    except (Exception,KeyboardInterrupt) as caught:
        if isinstance(caught,AutomationError):error=caught
        elif isinstance(caught,KeyboardInterrupt):error=AutomationError('INTERRUPTED','Task interrupted; tab retained for resume',exit_code=130)
        elif isinstance(caught,(APIError,TransportError)):error=AutomationError('API_UNAVAILABLE',str(caught),exit_code=7 if state and state.data.get('submit_intent') else 3)
        elif isinstance(caught,(OSError,ValueError)):error=AutomationError('LOCAL_IO_ERROR',str(caught),exit_code=2)
        else:error=AutomationError('UNEXPECTED_ERROR',f'{type(caught).__name__}: {caught}',exit_code=4)
        code=error.exit_code
        if writable:
            with contextlib.suppress(Exception):state.save(status='failed',error={'code':error.code,'message':str(error)})
            with contextlib.suppress(Exception):workflow.diagnostics()
        result=summarize(state,error)
    print(json.dumps(result,ensure_ascii=False,indent=None if args.json else 2));return code
