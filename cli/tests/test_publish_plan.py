from datetime import datetime
import contextlib
import io
import hashlib
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch
from cli.common.scheduling import ZONE
from cli.publish_plan import next_slot,run,replan,reconcile,write,main
from cli.common.automation import AutomationError


class PlanTests(unittest.TestCase):
    def test_plan_path_is_required_before_any_work(self):
        with contextlib.redirect_stderr(io.StringIO()), patch('cli.publish_plan.load') as load:
            with self.assertRaises(SystemExit) as error:
                main(['status'])
        self.assertEqual(error.exception.code,2)
        load.assert_not_called()

    def test_reconciliation_uses_literal_configured_series_marker(self):
        p={'series':'Example [Series]','platforms':{'douyin':{'account':'test-account'}},
           'episodes':[{'episode':2,'platforms':{'douyin':{'status':'planned'}}}],'history':[]}
        response={'success':True,'complete':True,'posts':[
            {'text':'Other series 002','body':'Other series 002 Published'},
            {'text':'Example [Series] 002','body':'Example [Series] 002 Published'}]}
        with patch('cli.publish_plan.invoke',return_value=(response,0)),patch('cli.publish_plan.write'):
            reconcile(Path('unused'),p,['douyin'])
        self.assertEqual(p['episodes'][0]['platforms']['douyin']['status'],'published')
        p.pop('series')
        with patch('cli.publish_plan.invoke') as invoke:
            with self.assertRaises(AutomationError) as error:
                reconcile(Path('unused'),p,['douyin'])
        self.assertEqual(error.exception.code,'INVALID_PLAN')
        invoke.assert_not_called()

    def test_generated_instructions_do_not_embed_machine_paths(self):
        with tempfile.TemporaryDirectory() as directory:
            path=Path(directory)/'publishing-plan.json'
            p={'series':'Example','series_name':'Example series',
               'platforms':{'douyin':{},'wechat_channels':{}},'episodes':[]}
            write(path,p)
            report=path.with_name('PUBLISHING_PLAN.md').read_text()
            self.assertIn('# Publication plan',report)
            self.assertIn('--plan /path/to/publishing-plan.json',report)
            self.assertNotIn(str(path.parent),report)
            self.assertNotIn(str(Path(__file__).resolve().parents[2]),report)

    def test_replan_three_slots_preserves_accepted_and_episode_order(self):
        p={'platforms':{'douyin':{}},'history':[],'cadence':{'local_times':['12:30','19:30']},'episodes':[
            {'episode':1,'platforms':{'douyin':{'status':'scheduled','scheduled_at':'2026-11-03T19:30:00+08:00','post_id':'accepted'}}},
            {'episode':2,'platforms':{'douyin':{'status':'planned','scheduled_at':'2026-12-03T12:30:00+08:00'}}},
            {'episode':3,'platforms':{'douyin':{'status':'planned','scheduled_at':'2026-12-03T19:30:00+08:00'}}},
            {'episode':4,'platforms':{'douyin':{'status':'planned','scheduled_at':'2026-12-04T12:30:00+08:00'}}}]}
        before=dict(p['episodes'][0]['platforms']['douyin'])
        with patch('cli.publish_plan.now',return_value=datetime(2026,10,26,17,tzinfo=ZONE)),patch('cli.publish_plan.write'):
            result=replan(Path('unused'),p,['douyin'],['19:30','09:30','12:30'])
        self.assertTrue(result['success'])
        self.assertEqual(p['episodes'][0]['platforms']['douyin'],before)
        self.assertEqual([r['platforms']['douyin']['scheduled_at'] for r in p['episodes'][1:]],[
            '2026-11-04T09:30:00+08:00','2026-11-04T12:30:00+08:00','2026-11-04T19:30:00+08:00'])

    def test_replan_refuses_unresolved_submission(self):
        p={'platforms':{'douyin':{}},'episodes':[{'episode':1,'platforms':{'douyin':{'status':'uncertain','state_file':'job.json'}}}]}
        with self.assertRaises(AutomationError) as error:replan(Path('unused'),p,['douyin'],['09:30','12:30','19:30'])
        self.assertEqual(error.exception.code,'UNRESOLVED_JOB')

    def test_two_daily_slots_skip_occupied_and_lead_time(self):
        record={'platforms':{'douyin':{'scheduled_at':'2026-10-20T12:30:00+08:00','status':'planned'}}}
        p={'cadence':{'local_times':['12:30','19:30']},'episodes':[record]}
        with patch('cli.publish_plan.now',return_value=datetime(2026,10,20,11,tzinfo=ZONE)):
            self.assertEqual(next_slot(p,'douyin',record).hour,19)
            p['episodes'].append({'platforms':{'douyin':{'status':'scheduled','scheduled_at':'2026-10-20T19:30:00+08:00'}}})
            self.assertEqual(next_slot(p,'douyin',record).isoformat(),'2026-10-21T12:30:00+08:00')

    def test_batch_count_and_failure_stop_for_platform(self):
        with tempfile.TemporaryDirectory() as directory:
            video=Path(directory)/'video.mp4';video.write_bytes(b'video')
            p={'platforms':{'douyin':{'account':'a'}},'history':[], 'episodes':[], 'cadence':{'local_times':['12:30','19:30']}}
            for i in range(1,5):
                p['episodes'].append({'episode':i,'video':str(video),'sha256':hashlib.sha256(b'video').hexdigest(),'title':'title','description':'description','hashtags':'tags','platforms':{'douyin':{'status':'planned','scheduled_at':'2026-10-20T12:30:00+08:00'}}})
            def submit(platform,args):
                slot=args[args.index('--schedule-at')+1];state=args[args.index('--state-file')+1]
                return {'success':True,'stage':'scheduled','scheduled_at':slot,'state_file':state},0
            with patch('cli.publish_plan.reconcile'),patch('cli.publish_plan.write'),patch('cli.publish_plan.schedule_time'),patch('cli.publish_plan.now',return_value=datetime(2026,10,19,16,tzinfo=ZONE)),patch('cli.publish_plan.invoke',side_effect=submit) as invoke:
                result=run(Path(directory)/'plan.json',p,['douyin'],2)
                self.assertTrue(result['success']);self.assertEqual(invoke.call_count,2)
                self.assertEqual([r['platforms']['douyin']['status'] for r in p['episodes']],['scheduled','scheduled','planned','planned'])
            with patch('cli.publish_plan.reconcile'),patch('cli.publish_plan.write'),patch('cli.publish_plan.schedule_time'),patch('cli.publish_plan.now',return_value=datetime(2026,10,19,16,tzinfo=ZONE)),patch('cli.publish_plan.invoke',return_value=({'success':False},7)) as invoke:
                self.assertFalse(run(Path(directory)/'plan.json',p,['douyin'],2)['success'])
                self.assertEqual(invoke.call_count,1)
                self.assertEqual(p['episodes'][3]['platforms']['douyin']['status'],'planned')

    def test_window_limit_stops_before_creating_a_job(self):
        with tempfile.TemporaryDirectory() as directory:
            video=Path(directory)/'v.mp4';video.write_bytes(b'video')
            p={'platforms':{'douyin':{'account':'a'}},'history':[], 'episodes':[{'episode':40,'video':str(video),'sha256':hashlib.sha256(b'video').hexdigest(),'platforms':{'douyin':{'status':'planned','scheduled_at':'2026-11-10T12:30:00+08:00'}}}]}
            with patch('cli.publish_plan.reconcile'),patch('cli.publish_plan.write'),patch('cli.publish_plan.now',return_value=datetime(2026,10,20,18,tzinfo=ZONE)),patch('cli.publish_plan.invoke') as invoke:
                result=run(Path(directory)/'plan.json',p,['douyin'],40)
                self.assertTrue(result['success']);self.assertTrue(result['limited']);invoke.assert_not_called()
                self.assertEqual(p['episodes'][0]['platforms']['douyin']['status'],'planned')
                self.assertNotIn('state_file',p['episodes'][0]['platforms']['douyin'])
