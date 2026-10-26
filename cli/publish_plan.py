"""Reconcile a persistent publication plan and schedule episodes on each platform."""
import argparse
from collections import Counter
from datetime import datetime,timedelta
import hashlib
import json
from pathlib import Path
import re
import subprocess
import sys
import uuid

if __package__ in {None, ''}:
    sys.path.insert(0,str(Path(__file__).resolve().parents[1]))
    __package__='cli'

from .common.automation import AutomationError,FileLock,atomic_json
from .common.scheduling import ZONE,schedule_time
from .common.publications import publication,observed_time

MODULES={'wechat_channels':'cli.wechat_channels','douyin':'cli.douyin'}
REPO=Path(__file__).resolve().parents[1]


def now():return datetime.now(ZONE)


def load(path):
    try:p=json.loads(path.read_text())
    except (OSError,ValueError) as e:raise AutomationError('INVALID_PLAN',str(e),exit_code=2)
    if p.get('schema_version')!=1 or p.get('timezone')!='Asia/Shanghai' or not isinstance(p.get('episodes'),list) or set(p.get('platforms',{}))-set(MODULES):
        raise AutomationError('INVALID_PLAN','Unsupported plan schema, timezone or platform',exit_code=2)
    for r in p['episodes']:
        if not isinstance(r.get('episode'),int) or not isinstance(r.get('platforms'),dict) or any(k not in r for k in ['video','sha256','title','description','hashtags']):
            raise AutomationError('INVALID_PLAN','Invalid episode fields',exit_code=2)
    if len({r['episode'] for r in p['episodes']})!=len(p['episodes']):raise AutomationError('INVALID_PLAN','Duplicate episode number',exit_code=2)
    return p


def counts(p):
    return {platform:dict(Counter(r['platforms'][platform]['status'] for r in p['episodes'])) for platform in p['platforms']}


def write(path,p):
    p['updated_at']=now().isoformat();atomic_json(path,p)
    times='、'.join(p.get('cadence',{}).get('local_times',[p.get('cadence',{}).get('local_time','19:30')]))
    lines=['# Publication plan','',f"Series: {p['series_name']} ({p['series']}); {len(p['episodes'])} episodes.",'',f'Daily slots (Asia/Shanghai): **{times}**. Each run submits one episode per platform by default; use `--per-platform N` for a batch. `planned` is a local candidate; `scheduled` means the platform accepted it.','',f'Source of truth: `{path.name}`. This Markdown is generated from JSON.','','## Commands','','```bash','python -m cli.publish_plan run --plan /path/to/publishing-plan.json','python -m cli.publish_plan sync --plan /path/to/publishing-plan.json','```','','Run from the repository root. Reconcile published and scheduled works before uploading videos whose SHA-256 matches the plan. Save each confirmed platform result immediately. Resume uncertain jobs to verify them before submitting more content.','','Scheduling uses the platforms themselves; the CLI does not install background tasks.','','## Episode status','','| Episode | Title | WeChat Channels | Douyin |','|---|---|---|---|']
    summary=counts(p)
    lines[4:4]=['Confirmed schedules: '+ '; '.join(f"{platform}: {values.get('scheduled',0)}" for platform,values in summary.items())+'.','']
    for r in sorted(p['episodes'],key=lambda r:r['episode']):
        statuses=[]
        for platform in ['wechat_channels','douyin']:
            s=r['platforms'][platform];t=(s.get('published_at') if s['status']=='published' else s.get('scheduled_at')) or ''
            statuses.append(s['status']+(' '+t[:16].replace('T',' ') if t else ''))
        lines.append(f"| {r['episode']:03} | {r['title'].replace('|','/')} | {statuses[0]} | {statuses[1]} |")
    lines+=['','## Recovery rules','','- Do not resubmit `published` or `scheduled` works or change their visibility.','- Resume `in_progress` or `uncertain` jobs using their `state_file` before proceeding.','- Roll expired candidates forward to an available daily slot; do not publish immediately.','- Validate each candidate against platform lead times and scheduling windows.','- A later `sync` updates accepted schedules to the observed published status.','']
    path.with_name('PUBLISHING_PLAN.md').write_text('\n'.join(lines))


def invoke(platform,args):
    command=[sys.executable,'-m',MODULES[platform],*args,'--json','--quiet']
    process=subprocess.run(command,cwd=REPO,text=True,capture_output=True)
    try:result=json.loads(process.stdout)
    except ValueError:raise AutomationError('CLI_RESPONSE_INVALID',f'{platform}: no structured result; {process.stderr[-800:]}',exit_code=7)
    return result,process.returncode


def remote_time(text):return observed_time(text)


def reconcile(path,p,platforms):
    series=p.get('series')
    if not isinstance(series,str) or not series.strip():
        raise AutomationError('INVALID_PLAN','A nonempty series marker is required for reconciliation',exit_code=2)
    pattern=re.compile(re.escape(series)+r'\s*0*(\d+)(?!\d)')
    for platform in platforms:
        result,code=invoke(platform,['recent','--account',p['platforms'][platform]['account'],'--require-complete'])
        if code or not result.get('success') or not result.get('complete'):
            raise AutomationError('AUDIT_FAILED',f'{platform}: reconciliation failed or content list is incomplete',exit_code=7,details=result)
        seen={}
        for post in result['posts']:
            match=pattern.search(post['text'])
            if not match:continue
            n=int(match.group(1));seen.setdefault(n,[]).append(post)
        for r in p['episodes']:
            posts=seen.get(r['episode'],[])
            if len(posts)>1:raise AutomationError('DUPLICATE_REMOTE_ITEMS',f'{platform}: multiple works match episode {r["episode"]}; manual review required',exit_code=7)
            if not posts:
                if r['platforms'][platform]['status'] in {'scheduled','published'}:
                    raise AutomationError('REMOTE_ITEM_MISSING',f'{platform}: submitted episode {r["episode"]} is missing from the complete list; stopped to prevent resubmission',exit_code=7)
                continue
            post=publication(posts[0],current=now());s=r['platforms'][platform]
            status=post['status'] if post['status'] in {'scheduled','published'} else 'needs_attention'
            old=s['status'];s['status']=status;s['evidence']={'kind':'observed_platform_list','item':post,'checked_at':now().isoformat(),'platform_status':post['platform_status']}
            if post.get('scheduled_at'):s['scheduled_at']=post['scheduled_at']
            if post.get('published_at'):s['published_at']=post['published_at']
            if s.get('state_file') and Path(s['state_file']).exists():
                task=json.loads(Path(s['state_file']).read_text())
                if task.get('status')=='completed':
                    s['post_id']=task.get('post_id')
                    s['last_result']={'success':True,'status':'completed','stage':task.get('phase'),
                                      'scheduled_at':task.get('scheduled_at'),'state_file':s['state_file'],
                                      'evidence':task.get('evidence'),'post_id':task.get('post_id')}
            if old!=status:p['history'].append({'at':now().isoformat(),'event':'reconciled','platform':platform,'episode':r['episode'],'from':old,'to':status})
        p['platforms'][platform]['audited_at']=now().isoformat()
        p['platforms'][platform]['observed_scheduled_slots']=[post['scheduled_at'] for raw in result['posts'] if (post:=publication(raw,current=now())).get('scheduled_at')]
        write(path,p)


def next_slot(p,platform,record):
    lead=timedelta(hours=2,minutes=5) if platform=='douyin' else timedelta(minutes=5)
    earliest=now()+lead
    occupied={datetime.fromisoformat(s['scheduled_at']) for r in p['episodes'] if (s:=r['platforms'][platform]).get('status')=='scheduled' and s.get('scheduled_at')}
    occupied.update(datetime.fromisoformat(t) for t in p.get('platforms',{}).get(platform,{}).get('observed_scheduled_slots',[]))
    slot=datetime.fromisoformat(record['platforms'][platform]['scheduled_at'])
    cadence=p.get('cadence',{})
    times=sorted(cadence.get('local_times',[cadence.get('local_time',slot.strftime('%H:%M'))]))
    # The planned date is a lower bound; never move an accepted platform task.
    lower=max(slot,earliest)
    day=lower.replace(hour=0,minute=0,second=0,microsecond=0)
    for offset in range(367):
        for value in times:
            hour,minute=map(int,value.split(':'))
            candidate=(day+timedelta(days=offset)).replace(hour=hour,minute=minute)
            if candidate>=lower and candidate not in occupied:return candidate
    raise AutomationError('NO_SCHEDULE_SLOT','No free schedule slot within one year',exit_code=2)


def replan(path,p,platforms,local_times):
    """Rebase only unsubmitted candidates after accepted work; never edit remote jobs."""
    if not local_times or any(not re.fullmatch(r'(?:[01]\d|2[0-3]):[0-5]\d',t) for t in local_times) or len(set(local_times))!=len(local_times):
        raise AutomationError('INVALID_CADENCE','Daily times must be unique HH:MM values',exit_code=2)
    times=sorted(local_times)
    if set(platforms)!=set(p['platforms']):
        raise AutomationError('INVALID_CADENCE','Cadence is shared; replan must include all platforms',exit_code=2)
    # An unresolved submission owns its original timestamp and must be reconciled first.
    for r in p['episodes']:
        for platform in platforms:
            s=r['platforms'][platform]
            if s['status'] not in {'planned','scheduled','published'} or (s['status']=='planned' and s.get('state_file')):
                raise AutomationError('UNRESOLVED_JOB',f'{platform} episode {r["episode"]}: resolve existing job before replanning',exit_code=7)
    before=dict(p.get('cadence',{}));changes={}
    p.setdefault('cadence',{}).update(local_times=times,days=1)
    p['cadence'].pop('local_time',None)
    for platform in platforms:
        lead=timedelta(hours=2,minutes=5) if platform=='douyin' else timedelta(minutes=5)
        cursor=now()+lead
        accepted=[datetime.fromisoformat(s['scheduled_at']) for r in p['episodes'] if (s:=r['platforms'][platform]).get('scheduled_at') and s['status'] in {'scheduled','published'}]
        accepted.extend(datetime.fromisoformat(t) for t in p['platforms'][platform].get('observed_scheduled_slots',[]))
        if accepted:cursor=max(cursor,max(accepted)+timedelta(minutes=1))
        changed=[]
        for r in sorted(p['episodes'],key=lambda r:r['episode']):
            s=r['platforms'][platform]
            if s['status']!='planned':continue
            s['scheduled_at']=cursor.isoformat()
            candidate=next_slot(p,platform,r)
            s['scheduled_at']=candidate.isoformat();cursor=candidate+timedelta(minutes=1)
            changed.append({'episode':r['episode'],'scheduled_at':s['scheduled_at']})
        changes[platform]=changed
    p['history'].append({'at':now().isoformat(),'event':'cadence_replanned','from':before,'to':dict(p['cadence']),'changes':changes,'platform_submission':False})
    write(path,p)
    return {'success':True,'local_times':times,'replanned':changes,'platform_submission':False,'counts':counts(p)}


def run(path,p,platforms,per_platform=1,through_episode=None):
    reconcile(path,p,platforms)
    outcomes=[]
    for platform in platforms:
        for _ in range(per_platform):
            record=next((r for r in sorted(p['episodes'],key=lambda r:r['episode']) if (through_episode is None or r['episode']<=through_episode) and r['platforms'][platform]['status'] not in {'published','scheduled'}),None)
            if not record:outcomes.append({'platform':platform,'status':'all_done'});break
            s=record['platforms'][platform]
            if s['status']=='needs_attention':
                raise AutomationError('PLATFORM_REQUIRES_ATTENTION',f'{platform}: episode {record["episode"]} requires manual review',exit_code=7)
            if s['status'] in {'in_progress','uncertain','failed'} and s.get('state_file'):
                if not Path(s['state_file']).exists():raise AutomationError('JOB_STATE_MISSING',f"Check {s['state_file']}; a new job will not be created automatically",exit_code=7)
                result,code=invoke(platform,['resume','--state-file',s['state_file']])
            else:
                video=Path(record['video'])
                if not video.is_file() or hashlib.sha256(video.read_bytes()).hexdigest()!=record['sha256']:raise AutomationError('VIDEO_CHANGED',f'{video} does not match the planned checksum',exit_code=2)
                slot=next_slot(p,platform,record)
                max_days=14 if platform=='douyin' else p['platforms'][platform].get('max_schedule_days')
                latest=now()+timedelta(days=max_days) if max_days else None
                if latest and slot>latest:
                    stop={'platform':platform,'status':'schedule_window_limit','episode':record['episode'],'scheduled_at':slot.isoformat(),'latest_allowed':latest.isoformat()}
                    outcomes.append(stop);p['history'].append({'at':now().isoformat(),'event':'schedule_window_limit',**stop});write(path,p)
                    print(f'{platform}: scheduling window reached; further submissions stopped',file=sys.stderr,flush=True);break
                schedule_time(slot.isoformat(),min_lead=7200 if platform=='douyin' else 120,max_days=max_days)
                job=path.parent/'publishing-jobs'/f'{platform}-{record["episode"]:03}-{uuid.uuid4().hex}.json'
                s.update(status='in_progress',scheduled_at=slot.isoformat(),state_file=str(job))
                p['history'].append({'at':now().isoformat(),'event':'submit_intent','platform':platform,'episode':record['episode'],'schedule_at':slot.isoformat(),'state_file':str(job)})
                write(path,p)
                description=(record['title']+'\n' if platform=='wechat_channels' else '')+record['description']+'\n'+record['hashtags']
                args=['post','--video',str(video),'--title',record.get('wechat_title',record['title']) if platform=='wechat_channels' else record['title'],'--description',description,'--account',p['platforms'][platform]['account'],'--schedule-at',slot.isoformat(),'--state-file',str(job)]
                if platform=='douyin':args+=['--visibility','public','--reuse-draft']
                result,code=invoke(platform,args)
            s['last_result']=result
            if code==0 and result.get('success') and result.get('stage')=='scheduled':
                s.update(status='scheduled',scheduled_at=result['scheduled_at'],evidence=result.get('evidence'),state_file=result.get('state_file'),post_id=result.get('post_id'))
                p['history'].append({'at':now().isoformat(),'event':'schedule_confirmed','platform':platform,'episode':record['episode'],'scheduled_at':s['scheduled_at']})
            else:
                s['status']='uncertain';p['history'].append({'at':now().isoformat(),'event':'submission_not_verified','platform':platform,'episode':record['episode'],'result':result})
            write(path,p);outcomes.append({'platform':platform,'episode':record['episode'],'result':result})
            print(f"{platform} episode {record['episode']:03}: {s['status']}",file=sys.stderr,flush=True)
            if s['status']!='scheduled':break
    return {'success':all(o.get('status') in {'all_done','schedule_window_limit'} or (o.get('result',{}).get('success') and o.get('result',{}).get('stage')=='scheduled') for o in outcomes),'limited':any(o.get('status')=='schedule_window_limit' for o in outcomes),'outcomes':outcomes,'counts':counts(p),'plan':str(path)}


def main(argv=None):
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('command',choices=['status','sync','replan','run'])
    parser.add_argument('--plan',type=Path,required=True,help='Publication plan JSON file')
    parser.add_argument('--platform',choices=MODULES,action='append',help='All configured platforms by default; repeatable')
    parser.add_argument('--per-platform',type=int,default=1,help='New submissions per platform for run; default: 1; skip accepted schedules')
    parser.add_argument('--all',action='store_true',help='Submit all remaining episodes; stop a platform on a limit or uncertain outcome')
    parser.add_argument('--through-episode',type=int,help='Process episodes up to this number')
    parser.add_argument('--daily-times',nargs='+',help='Daily Shanghai times (HH:MM) for replan; preserve accepted schedules')
    args=parser.parse_args(argv);path=args.plan.expanduser().resolve()
    if not 1<=args.per_platform<=20:parser.error('--per-platform must be between 1 and 20')
    if args.through_episode is not None and args.through_episode<1:parser.error('--through-episode must be positive')
    if (args.command=='replan')!=bool(args.daily_times):parser.error('--daily-times is required for replan and only valid with replan')
    try:
        with FileLock(str(path)+'.lock'):
            p=load(path);platforms=list(dict.fromkeys(args.platform or p['platforms']))
            if args.command=='run':result=run(path,p,platforms,len(p['episodes']) if args.all else args.per_platform,args.through_episode)
            elif args.command=='replan':
                reconcile(path,p,platforms)
                result=replan(path,p,platforms,args.daily_times)
            else:
                if args.command=='sync':reconcile(path,p,platforms)
                write(path,p)
                result={'success':True,'plan':str(path),'counts':counts(p),'episodes':[{ 'episode':r['episode'],'platforms':r['platforms']} for r in p['episodes']]}
        print(json.dumps(result,ensure_ascii=False,indent=2));return 0 if result['success'] else 7
    except Exception as e:
        print(json.dumps({'success':False,'code':getattr(e,'code','PLAN_FAILED'),'message':str(e),'details':getattr(e,'details',{})},ensure_ascii=False));return getattr(e,'exit_code',7)


if __name__=='__main__':
    raise SystemExit(main())
