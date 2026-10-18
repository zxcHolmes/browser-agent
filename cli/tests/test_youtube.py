"""Offline safety/recovery tests. Real browser fixture: youtube_smoke.py."""
import contextlib
import io
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from cli.automation import AutomationError, TaskState, FileLock, claim_upload
from cli.browser_client import APIError, TransportError
from cli.youtube import YouTubeWorkflow, parser_for_cli, prepare_upload, main, same_text

CHANNEL='UC'+'0'*22

class WorkflowTests(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory(); self.addCleanup(self.temp.cleanup)
        self.root=Path(self.temp.name);self.video=self.root/'video.mp4';self.video.write_bytes(b'fixture')
        self.args=['upload','--video',str(self.video),'--title','Test title','--state-dir',str(self.root),'--quiet']
        self.state=prepare_upload(parser_for_cli().parse_args(self.args))
        self.state.save(channel_id=CHANNEL)

    def workflow(self, **kwargs):
        from unittest.mock import Mock
        client=Mock()
        return YouTubeWorkflow(client,self.state,quiet=True,**kwargs)

    def assert_code(self, code, callback):
        with self.assertRaises(AutomationError) as caught: callback()
        self.assertEqual(caught.exception.code,code)

    def test_login_and_challenge(self):
        w=self.workflow()
        for key,code in [('loginRequired','LOGIN_REQUIRED'),('challenge','VERIFICATION_REQUIRED'),('accessDenied','CHANNEL_ACCESS_DENIED'),('channelSetupRequired','CHANNEL_SETUP_REQUIRED')]:
            self.assert_code(code,lambda:w.guard({key:True}))

    def test_rejected_upload(self):
        self.assert_code('YOUTUBE_REJECTED_UPLOAD',lambda:self.workflow().guard({'alerts':['上传失败，文件格式无效']}))

    def test_ambiguous_mutation_never_retried(self):
        w=self.workflow();w.client.action.side_effect=TransportError('lost reply')
        self.assert_code('ACTION_OUTCOME_UNKNOWN',lambda:w.mutate('publish','click',target='#done'))
        self.assertEqual(w.client.action.call_count,1)
        self.assertEqual(TaskState.load(self.state.path).data['last_action']['outcome'],'pending')

    def test_video_id_mismatch(self):
        self.state.save(video_id='oldVideo001')
        w=self.workflow();w.client.evaluate.return_value={'videoIds':['newVideo001']}
        self.assert_code('VIDEO_MISMATCH',w.probe)

    def test_timeout_has_observation(self):
        now=[0];w=self.workflow(clock=lambda:now[0],sleep=lambda n:now.__setitem__(0,now[0]+n))
        w.client.evaluate.return_value={'progress':'Uploading 5%'}
        self.assert_code('UPLOAD_TIMEOUT',lambda:w.poll(lambda p:False,2,'UPLOAD_TIMEOUT','slow'))
        self.assertEqual(w.last_probe['progress'],'Uploading 5%')

    def test_duplicate_upload(self):
        claim_upload(self.state,self.root)
        other=prepare_upload(parser_for_cli().parse_args(self.args));other.save(channel_id=CHANNEL)
        self.assert_code('DUPLICATE_UPLOAD',lambda:claim_upload(other,self.root))
        claim_upload(other,self.root,allow_duplicate=True)

    def test_lock_failure_does_not_modify_task(self):
        self.state.save();before=self.state.path.read_bytes()
        with FileLock(str(self.state.path)+'.lock'),contextlib.redirect_stdout(io.StringIO()):
            rc=main(['resume','--state-file',str(self.state.path),'--quiet'])
        self.assertEqual(rc,4);self.assertEqual(before,self.state.path.read_bytes())

    def test_existing_state_not_overwritten(self):
        self.state.save();before=self.state.path.read_bytes()
        with contextlib.redirect_stdout(io.StringIO()):
            rc=main(self.args+['--state-file',str(self.state.path)])
        self.assertEqual(rc,2);self.assertEqual(before,self.state.path.read_bytes())

    def test_lost_tab_without_id_never_reuploads(self):
        self.state.save(upload_intent=True,tab_id=123)
        w=self.workflow();w.client.capabilities.return_value={'implementationRevision':6,'actions':['upload_files','fill','click','snapshot','set_tab_policy']}
        w.client.tab.side_effect=APIError(404,'TAB_NOT_FOUND','gone')
        self.assert_code('UPLOAD_OUTCOME_UNKNOWN',lambda:w.attach(True))
        w.client.create_tab.assert_not_called()

    def test_publish_unknown_only_reconciles(self):
        self.state.data['config'].update(mode='publish',audience='not-made-for-kids')
        self.state.save(publish_intent=True,upload_intent=True,video_id='TestVideo01')
        w=self.workflow();w.attach=lambda recovering:None
        w.client.evaluate.return_value={'rows':[{'videoIds':['TestVideo01'],'draft':False,'visibility':'public','title':'Test title'}]}
        w.run(recovering=True)
        self.assertEqual(self.state.data['phase'],'published');w.client.action.assert_not_called()

    def test_publish_unknown_draft_does_not_retry(self):
        self.state.save(publish_intent=True,upload_intent=True,video_id='TestVideo01')
        w=self.workflow();w.attach=lambda recovering:None
        w.client.evaluate.return_value={'rows':[{'videoIds':['TestVideo01'],'draft':True}]}
        self.assert_code('PUBLISH_OUTCOME_UNKNOWN',lambda:w.run(True));w.client.action.assert_not_called()

    def test_publish_requires_audience(self):
        self.assert_code('AUDIENCE_REQUIRED',lambda:prepare_upload(parser_for_cli().parse_args(self.args+['--mode','publish'])))

    def test_title_file_and_numeric_validation(self):
        title=self.root/'title.txt';title.write_text('中文标题',encoding='utf-8')
        args=parser_for_cli().parse_args(['upload','--video',str(self.video),'--title-file',str(title),'--quiet'])
        self.assertEqual(prepare_upload(args).data['config']['title'],'中文标题')
        args.poll_interval=float('nan');self.assert_code('INVALID_ARGUMENT',lambda:prepare_upload(args))

    def test_changed_video_rejected(self):
        w=self.workflow();self.video.write_bytes(b'changed content')
        self.assert_code('VIDEO_CHANGED',w.select_video);w.client.action.assert_not_called()

    def test_blocked_step_does_not_click(self):
        w=self.workflow();w.client.evaluate.return_value={'actions':{'next':{'enabled':False}}}
        self.assert_code('PUBLISH_BLOCKED',w.navigate_visibility);w.client.action.assert_not_called()

    def test_same_step_not_clicked_twice(self):
        now=[0];w=self.workflow(clock=lambda:now[0],sleep=lambda n:now.__setitem__(0,now[0]+n))
        self.state.data['config']['step_timeout']=1
        w.client.evaluate.return_value={'step':'details','actions':{'next':{'enabled':True,'target':'#next'}}}
        self.assert_code('STEP_TRANSITION_TIMEOUT',w.navigate_visibility);self.assertEqual(w.client.action.call_count,1)


class DownloadAndClientTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        from http.server import BaseHTTPRequestHandler,ThreadingHTTPServer
        import threading
        class Handler(BaseHTTPRequestHandler):
            def do_GET(self):
                body={'/video':b'video-content','/html':b'<html>sign in</html>',
                      '/bad-json':b'not json','/bad-error':b'[]'}.get(self.path,b'{}')
                self.send_response(500 if self.path=='/bad-error' else 200)
                self.send_header('Content-Type','text/html' if self.path=='/html' else 'application/octet-stream')
                self.send_header('Content-Length',str(len(body)));self.end_headers();self.wfile.write(body)
            def log_message(self,*_):pass
        cls.server=ThreadingHTTPServer(('127.0.0.1',0),Handler)
        threading.Thread(target=cls.server.serve_forever,daemon=True).start()
        cls.base=f'http://127.0.0.1:{cls.server.server_port}'
    @classmethod
    def tearDownClass(cls):cls.server.shutdown();cls.server.server_close()

    def test_direct_download_and_html_rejection(self):
        from cli.youtube import download_video
        with tempfile.TemporaryDirectory() as temp:
            path=download_video(self.base+'/video',temp,quiet=True)
            self.assertEqual(path.read_bytes(),b'video-content')
            with self.assertRaises(AutomationError) as caught:download_video(self.base+'/html',temp,quiet=True)
            self.assertEqual(caught.exception.code,'DOWNLOAD_FAILED')
            self.assertFalse((Path(temp)/'html.part').exists())

    def test_client_malformed_json_and_http_error(self):
        from cli.browser_client import BrowserClient
        c=BrowserClient(self.base)
        with self.assertRaises(TransportError):c.request('/bad-json')
        with self.assertRaises(APIError) as caught:c.request('/bad-error')
        self.assertEqual(caught.exception.status,500)

    def test_watch_url_is_not_direct_file(self):
        from cli.youtube import download_video
        with self.assertRaises(AutomationError) as caught:download_video('https://www.youtube.com/watch?v=TestVideo01','/unused')
        self.assertEqual(caught.exception.code,'NOT_DIRECT_VIDEO_URL')

class ContentFallbackTests(unittest.TestCase):
    setUp = WorkflowTests.setUp
    workflow = WorkflowTests.workflow
    # Only the additional branch; inherited fixture setup is reused.
    def test_content_shorts_fallback(self):
        w=self.workflow();w.state.save(video_id='TestVideo01')
        observations=[AutomationError('EXISTING_VIDEO_NOT_FOUND','not in videos'),{'rows':[{'videoIds':['TestVideo01'],'draft':True}]}]
        def poll(*args):
            item=observations.pop(0)
            if isinstance(item,Exception):raise item
            return item
        w.poll=poll;w.last_probe={'contentTabs':{'shorts':{'enabled':True,'target':'#shorts'}}}
        probe=w.load_content_row()
        self.assertEqual(w.find_row(probe)['draft'],True)
        self.assertEqual(w.client.action.call_args.kwargs['target'],'#shorts')

class RecentAndDuplicateTests(unittest.TestCase):
    setUp = WorkflowTests.setUp
    workflow = WorkflowTests.workflow
    assert_code = WorkflowTests.assert_code

    def rows_for(self, w, by_list):
        def evaluate(tab, probe):
            name=w.client.navigate.call_args.args[1].rsplit('/',1)[1]
            key={'upload':'videos','short':'shorts'}[name]
            return {'contentList':key,'rows':by_list.get(key,[])}
        w.client.evaluate.side_effect=evaluate

    def test_recent_reads_both_lists(self):
        w=self.workflow();now=[0];w.clock=lambda:now[0];w.sleep=lambda n:now.__setitem__(0,now[0]+n)
        self.rows_for(w,{'videos':[{'videoIds':['LongVideo01'],'title':'Long','visibility':'public','date':'Oct 1'}],
                         'shorts':[{'videoIds':['ShortVid001'],'title':'Short','draft':True}]})
        rows=w.recent_rows()
        self.assertEqual([(r['video_id'],r['list'],r['visibility']) for r in rows],
                         [('LongVideo01','videos','public'),('ShortVid001','shorts','draft')])

    def test_empty_list_is_not_an_error(self):
        w=self.workflow();now=[0];w.clock=lambda:now[0];w.sleep=lambda n:now.__setitem__(0,now[0]+n)
        self.rows_for(w,{})
        self.assertEqual(w.recent_rows(),[])

    def test_duplicate_title_blocks_before_upload(self):
        w=self.workflow();now=[0];w.clock=lambda:now[0];w.sleep=lambda n:now.__setitem__(0,now[0]+n)
        self.rows_for(w,{'shorts':[{'videoIds':['ShortVid001'],'title':'  test   TITLE ','draft':True}]})
        self.assert_code('DUPLICATE_TITLE',w.check_title_unused)
        w.client.action.assert_not_called()

    def test_trailing_editor_newline_ignored(self):
        self.assertTrue(same_text('a\n\nb\n','a\r\n\r\nb'))
        self.assertFalse(same_text('a\nb','a b'))

class ThumbnailTests(unittest.TestCase):
    setUp = WorkflowTests.setUp
    workflow = WorkflowTests.workflow
    assert_code = WorkflowTests.assert_code

    def fake_clock(self, w):
        now=[0];w.clock=lambda:now[0];w.sleep=lambda n:now.__setitem__(0,now[0]+n)

    def test_prepare_thumbnail(self):
        import shutil, subprocess
        from cli.youtube import prepare_thumbnail
        self.assertIsNone(prepare_thumbnail(None,None,None,self.root))
        png=self.root/'t.png';png.write_bytes(b'png-bytes')
        self.assertEqual(prepare_thumbnail(str(png),None,None,self.root),str(png.resolve()))
        self.assert_code('INVALID_ARGUMENT',lambda:prepare_thumbnail(None,1,None,self.root))
        if not shutil.which('ffmpeg'): self.skipTest('ffmpeg not installed')
        clip=self.root/'clip.mp4'
        subprocess.run(['ffmpeg','-v','error','-y','-f','lavfi','-i','color=c=red:s=64x64:d=2','-pix_fmt','yuv420p',str(clip)],check=True)
        frame=prepare_thumbnail(None,1,str(clip),self.root/'work')
        self.assertTrue(frame.endswith('.jpg') and Path(frame).stat().st_size>0)
        self.assert_code('FRAME_EXTRACT_FAILED',lambda:prepare_thumbnail(None,30,str(clip),self.root/'work'))

    def test_thumbnail_uploaded_once_and_verified(self):
        image=self.root/'t.jpg';image.write_bytes(b'jpeg')
        self.state.data['config']['thumbnail']=str(image)
        w=self.workflow();self.fake_clock(w)
        probes=[{'thumbnail':{'target':'#file-loader','enabled':True,'custom':False}},
                {'thumbnail':{'target':'#file-loader','enabled':True,'custom':True}}]
        w.client.evaluate.side_effect=lambda *a:probes[0] if len(probes)==1 else probes.pop(0)
        w.client.action.return_value={'files':[{'name':'t.jpg','size':4}]}
        w.set_thumbnail()
        self.assertEqual(w.client.action.call_count,1)
        self.assertEqual(w.client.action.call_args.kwargs['files'],[str(image)])
        self.assertTrue(self.state.data['thumbnail_set'])

    def test_thumbnail_not_allowed(self):
        image=self.root/'t.jpg';image.write_bytes(b'jpeg')
        self.state.data['config']['thumbnail']=str(image)
        w=self.workflow();self.fake_clock(w)
        w.client.evaluate.return_value={'thumbnail':{'target':'#file-loader','enabled':False,'custom':False,'text':'verify'}}
        self.assert_code('THUMBNAIL_NOT_ALLOWED',w.set_thumbnail)
        w.client.action.assert_not_called()

if __name__=='__main__':unittest.main()
