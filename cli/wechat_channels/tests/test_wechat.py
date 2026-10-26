from datetime import datetime,timedelta
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from cli.common.automation import AutomationError,TaskState
from cli.common.page_workflow import MemoryState
from cli.common.scheduling import schedule_time,ZONE
from cli.wechat_channels.main import parser_for_cli,prepare,WeChatWorkflow,short_title,resume_title
from cli.publish_plan import load,next_slot,reconcile


class WeChatTests(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory();self.addCleanup(self.temp.cleanup)
        self.root=Path(self.temp.name);self.video=self.root/'video.mp4';self.video.write_bytes(b'video')

    def test_prepare_string_file_and_scheduled_time(self):
        p=self.root/'title.txt';p.write_text('这是示例视频标题')
        date=(datetime.now(ZONE)+timedelta(days=1)).replace(hour=19,minute=30,second=0,microsecond=0)
        args=parser_for_cli().parse_args(['post','--video',str(self.video),'--title-file',str(p),'--description','两行\n简介','--schedule-at',date.isoformat(),'--state-dir',str(self.root)])
        s=prepare(args)
        self.assertEqual(s.data['config']['title'],'这是示例视频标题')
        self.assertEqual(s.data['config']['description'],'两行\n简介')
        self.assertEqual(s.data['config']['schedule_at'],date.isoformat())
        self.assertFalse(s.path.exists())

    def test_short_title_normalizes_commas_and_refuses_other_symbols(self):
        self.assertEqual(short_title('这是示例，视频内容？'),'这是示例 视频内容？')
        self.assertEqual(short_title('示例「引用文字」'),'示例“引用文字”')
        with self.assertRaises(AutomationError):short_title('标题/不支持')

    def test_resume_can_fix_title_before_submission_only(self):
        state=MemoryState({'status':'failed','config':{'title':'示例标题'}})
        resume_title(state,'这是修正后的示例标题')
        self.assertEqual(state.data['config']['title'],'这是修正后的示例标题')
        self.assertEqual(state.data['config_edits'][0]['from'],'示例标题')
        state.data['submit_intent']={'kind':'post'}
        with self.assertRaises(AutomationError) as caught:resume_title(state,'另一个有效的标题')
        self.assertEqual(caught.exception.code,'ALREADY_SUBMITTED')

    def test_scheduled_row_uses_actual_wechat_future_label(self):
        date=(datetime.now(ZONE)+timedelta(days=1)).replace(hour=19,minute=30,second=0,microsecond=0)
        state=MemoryState({'config':{'mode':'post','title':'示例标题','description':'Example Series 002 内容',
                                     'schedule_at':date.isoformat(),'step_timeout':2,'poll_interval':.1}})
        p={'account':'account','rows':[{'text':'Example Series 002 内容','body':'Example Series 002 内容 将于'+date.strftime('%Y年%m月%d日 %H:%M')+'发表'}]}
        w=WeChatWorkflow(None,state)
        with patch.object(w,'open',return_value=p),patch.object(w,'probe',return_value=p):w.verify_result()
        self.assertEqual(state.data['phase'],'scheduled')
        self.assertEqual(state.data['scheduled_at'],date.isoformat())

    def test_audit_reviewing_future_item_is_scheduled_not_published(self):
        date=(datetime.now(ZONE)+timedelta(days=1)).replace(hour=19,minute=30,second=0,microsecond=0)
        p={'series':'Example Series','platforms':{'douyin':{'account':'a'}},'episodes':[{'episode':2,'platforms':{'douyin':{'status':'planned','scheduled_at':date.isoformat()}}}],'history':[]}
        response={'success':True,'complete':True,'posts':[{'text':'Example Series 002 内容','body':'Example Series 002 '+date.strftime('%Y年%m月%d日 %H:%M')+' 审核中'}]}
        with patch('cli.publish_plan.invoke',return_value=(response,0)),patch('cli.publish_plan.write'):
            reconcile(self.root/'plan.json',p,['douyin'])
        self.assertEqual(p['episodes'][0]['platforms']['douyin']['status'],'scheduled')

    def test_paginated_audit_counts_all_pages(self):
        state=MemoryState({'config':{'step_timeout':2,'poll_interval':.1}})
        w=WeChatWorkflow(None,state)
        first={'account':'a','rows':[{'text':'one','body':'one'}],'total':2,'actions':{'nextPage':{'enabled':True,'target':'.next'}}}
        last={'account':'a','rows':[{'text':'two','body':'two'}],'total':2,'actions':{'nextPage':{'enabled':False,'target':'.next'}}}
        with patch.object(w,'attach'),patch.object(w,'open',return_value=first),patch.object(w,'probe',side_effect=[first,last]),patch.object(w,'mutate'),patch.object(w,'poll'):
            result=w.recent()
        self.assertTrue(result['complete'])
        self.assertEqual(len(result['posts']),2)

    def test_title_and_bad_schedule_rejected_before_browser(self):
        for argv in [['--title','长'*17],['--title','短标题'],['--title','有效标题六个字','--schedule-at','bad']]:
            args=parser_for_cli().parse_args(['post','--video',str(self.video),*argv])
            with self.assertRaises(AutomationError) as caught:prepare(args)
            self.assertEqual(caught.exception.exit_code,2)

    def test_future_validation_and_timezone_conversion(self):
        fixed=datetime(2026,10,19,15,0,tzinfo=ZONE)
        result=schedule_time('2026-10-19T11:30:00+00:00',now=fixed,min_lead=7200,max_days=14)
        self.assertEqual(result.hour,19)
        for value in ['2026-10-19T16:00:00+08:00','2026-11-20T19:30:00+08:00','2026-10-19T19:30:01+08:00']:
            with self.assertRaises(AutomationError):schedule_time(value,now=fixed,min_lead=7200,max_days=14)

    def test_guard_login_challenge_and_unexpected_dialog(self):
        state=MemoryState({'config':{'step_timeout':2,'poll_interval':.1}})
        w=WeChatWorkflow(None,state)
        for p,code in [({'loginRequired':True},'LOGIN_REQUIRED'),({'challenge':True},'VERIFICATION_REQUIRED'),({'dialogs':['请验证身份']},'DIALOG_REQUIRES_ATTENTION'),({'host':'other.test'},'UNEXPECTED_ORIGIN')]:
            with self.assertRaises(AutomationError) as caught:w.guard(p)
            self.assertEqual(caught.exception.code,code)

    def test_submit_intent_only_reconciles_on_resume(self):
        state=MemoryState({'config':{'step_timeout':2,'poll_interval':.1},'submit_intent':{'kind':'post'}})
        w=WeChatWorkflow(None,state)
        with patch.object(w,'attach'),patch.object(w,'verify_result') as verify,patch.object(w,'mutate') as mutate:
            w.run();verify.assert_called_once();mutate.assert_not_called()

    def test_expired_login_url_reports_login_required(self):
        state=MemoryState({'config':{}})
        with self.assertRaises(AutomationError) as caught:WeChatWorkflow(None,state).guard({'url':'https://channels.weixin.qq.com/login.html'})
        self.assertEqual(caught.exception.code,'LOGIN_REQUIRED')

    def test_wrong_account_stops(self):
        state=MemoryState({'config':{'account':'expected','step_timeout':2,'poll_interval':.1}})
        with self.assertRaises(AutomationError) as caught:WeChatWorkflow(None,state).account({'account':'other'})
        self.assertEqual(caught.exception.code,'ACCOUNT_MISMATCH')

    def test_plan_slot_rolls_forward_without_immediate_publication(self):
        fixed=datetime(2026,10,19,18,30,tzinfo=ZONE)
        record={'platforms':{'douyin':{'status':'planned','scheduled_at':'2026-10-19T19:30:00+08:00'}}}
        p={'episodes':[record]}
        with patch('cli.publish_plan.now',return_value=fixed):slot=next_slot(p,'douyin',record)
        self.assertEqual(slot.isoformat(),'2026-10-20T19:30:00+08:00')

    def test_plan_incomplete_audit_blocks_all_writes(self):
        path=self.root/'plan.json';p={'series':'Example Series','platforms':{'douyin':{'account':'a'}},'episodes':[]}
        with patch('cli.publish_plan.invoke',return_value=({'success':True,'complete':False},0)):
            with self.assertRaises(AutomationError):reconcile(path,p,['douyin'])
        self.assertFalse(path.exists())

    def test_plan_duplicate_episode_rejected(self):
        path=self.root/'plan.json';r={'episode':1,'platforms':{},'video':'x','sha256':'x','title':'t','description':'d','hashtags':''}
        path.write_text(json.dumps({'schema_version':1,'timezone':'Asia/Shanghai','platforms':{'douyin':{}},'episodes':[r,r]}))
        with self.assertRaises(AutomationError):load(path)

if __name__=='__main__':unittest.main()
