from datetime import datetime
import unittest
from cli.common.publications import publication,listing_result,observation_key,select_publications
from cli.common.scheduling import ZONE

class PublicationTests(unittest.TestCase):
    def test_scheduled_and_published_dates(self):
        p=publication({'text':'caption','body':'caption 将于2026年10月25日 12:30发表 删除'})
        self.assertEqual(p['status'],'scheduled');self.assertEqual(p['scheduled_at'],'2026-10-25T12:30:00+08:00')
        p=publication({'text':'caption','body':'caption 2026年10月19日 19:30 修改并重新发表 删除'},current=datetime(2026,10,20,tzinfo=ZONE))
        self.assertEqual(p['status'],'published')

    def test_caption_status_words_do_not_decide_platform_status(self):
        p=publication({'text':'发布失败怎么办','body':'发布失败怎么办 2026年10月19日 19:30 已发布'})
        self.assertEqual(p['status'],'published')
        self.assertEqual(publication({'text':'caption','body':'caption 审核未通过'})['status'],'rejected')
        self.assertEqual(publication({'text':'caption','body':'caption'})['status'],'unknown')

    def test_view_metrics_do_not_change_identity(self):
        self.assertEqual(observation_key({'text':'x','body':'x 2026年10月19日 19:30 已发布 播放 5'}),observation_key({'text':'x','body':'x 2026年10月19日 19:30 已发布 播放 9'}))

    def test_truncation_cannot_claim_complete(self):
        result=listing_result('a',[{'text':'x','body':'x 已发布'},{'text':'y','body':'y 定时发布'}],complete=True,limit=1,total=2)
        self.assertFalse(result['complete']);self.assertTrue(result['truncated'])
        result=listing_result('a',[{'text':'x','body':'x 已发布'},{'text':'y','body':'y 定时发布'}],complete=True,limit=10,total=2)
        self.assertEqual(select_publications(result,'scheduled')['count'],1)

    def test_douyin_scroll_reads_beyond_first_lazy_loaded_page(self):
        from unittest.mock import patch
        from cli.douyin.main import DouyinWorkflow
        from cli.common.page_workflow import MemoryState
        state=MemoryState({'account':'a','config':{'step_timeout':5,'poll_interval':.01}})
        w=DouyinWorkflow(None,state,sleep=lambda _:None)
        pages=[{'contentItems':[{'text':str(i),'body':str(i)} for i in range(n)],'total':42,'listEnd':False} for n in [12,24,42]]
        with patch.object(w,'ensure_tab'),patch.object(w,'check_account'),patch.object(w,'open'),patch.object(w,'probe',side_effect=pages),patch('cli.douyin.main.scroll_list') as scroll:
            result=w.recent()
        self.assertEqual(result['count'],42);self.assertTrue(result['complete']);self.assertEqual(scroll.call_count,2)
