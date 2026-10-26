"""Observed publication lists: stable identities, dates, status and completeness.

These helpers use only visible list evidence, never site tokens or internal state.
"""
from collections import Counter
from datetime import datetime
import re
from .scheduling import ZONE


def observed_time(text):
    match=re.search(r'(\d{4})[年/-](\d{1,2})[月/-](\d{1,2})日?\s*(\d{1,2}):(\d{2})',text or '')
    if not match:return None
    try:return datetime(*map(int,match.groups()),tzinfo=ZONE).isoformat()
    except ValueError:return None


def publication(item, *, current=None):
    result=dict(item);body=item.get('body','');date=observed_time(body)
    current=current or datetime.now(ZONE)
    # Strip the description before reading status: a caption may contain status words.
    evidence=body
    caption=item.get('text','')
    if caption and caption in body:evidence=body.split(caption,1)[1]
    if re.search(r'审核未通过|发布失败|违规|Rejected|Failed',evidence,re.I):status='rejected'
    elif re.search(r'定时发布|待发表|待发布|将于|Scheduled',evidence,re.I):status='scheduled'
    elif re.search(r'审核中|Reviewing|Under review',evidence,re.I):
        status='scheduled' if date and datetime.fromisoformat(date)>current else 'reviewing'
    elif re.search(r'已发布|已发表|Published',evidence,re.I) or (date and re.search(r'修改并重新发表|删除',evidence) and datetime.fromisoformat(date)<=current):status='published'
    elif re.search(r'草稿|Draft',evidence,re.I):status='draft'
    else:status='unknown'
    result.update(status=status,scheduled_at=date if status=='scheduled' else None,published_at=date if status=='published' else None,
                  platform_status='审核中' if re.search(r'审核中|Under review',evidence,re.I) else status)
    return result


def observation_key(item):
    if item.get('id'):return ('id',str(item['id']))
    if item.get('url'):return ('url',item['url'])
    return ('text_time',item.get('text',''),observed_time(item.get('body','')))


def listing_result(account,items,*,complete,limit,total=None):
    posts=[publication(item) for item in items]
    truncated=len(posts)>limit or (not complete and len(posts)>=limit)
    posts=posts[:limit]
    return {'success':True,'account':account,'posts':posts,'count':len(posts),'summary':dict(Counter(p['status'] for p in posts)),
            'complete':complete and not truncated,'truncated':truncated,'total_reported':total,'checked_at':datetime.now(ZONE).isoformat()}


def scroll_list(client,tab_id,item_selector):
    # The scrollable ancestor, rather than the last title, must reach the lazy-load sentinel.
    selector=repr(item_selector)
    return client.evaluate(tab_id,"""(() => {let e=document.querySelector(SELECTOR);while(e&&e!==document.body){
      if(e.clientHeight>0&&e.scrollHeight>e.clientHeight+10&&/(auto|scroll)/.test(getComputedStyle(e).overflowY)){
        e.scrollTo({top:e.scrollHeight,behavior:'instant'});return {top:e.scrollTop,height:e.clientHeight,total:e.scrollHeight};}
      e=e.parentElement;}const s=document.scrollingElement;s?.scrollTo({top:s.scrollHeight,behavior:'instant'});return {document:true};})()""".replace('SELECTOR',selector))


def select_publications(result,status=None,limit=200):
    result=dict(result)
    posts=[p for p in result['posts'] if not status or p['status']==status]
    truncated=result.get('truncated',False) or len(posts)>limit
    result.update(posts=posts[:limit],count=len(posts[:limit]),summary=dict(Counter(p['status'] for p in posts[:limit])),
                  complete=result['complete'] and not truncated,truncated=truncated)
    return result
