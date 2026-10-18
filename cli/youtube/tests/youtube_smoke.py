"""Opt-in real Chrome/Go/plugin integration; only uploads to localhost fixture.
Run from the repository root: python3 -m cli.youtube.tests.youtube_smoke
"""
import json
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
import tempfile
import threading

from cli.common.browser_client import BrowserClient, TransportError
from cli.youtube.main import YouTubeWorkflow, parser_for_cli, prepare_upload
from cli.common.automation import AutomationError

HTML=(Path(__file__).parent/'fixtures'/'youtube_studio.html').read_bytes()
class Handler(BaseHTTPRequestHandler):
    page=HTML
    def do_GET(self):
        self.send_response(200);self.send_header('Content-Type','text/html; charset=utf-8');self.end_headers();self.wfile.write(self.page)
    def log_message(self,*_):pass


def main():
    server=ThreadingHTTPServer(('127.0.0.1',0),Handler)
    threading.Thread(target=server.serve_forever,daemon=True).start()
    studio=f'http://127.0.0.1:{server.server_port}'
    client=BrowserClient();results=[];tabs=[]
    try:
        with tempfile.TemporaryDirectory(prefix='youtube-cli-smoke-') as temp:
            root=Path(temp);video=root/'sample video.mp4';video.write_bytes(b'local upload fixture bytes')
            def state_for(mode):
                args=parser_for_cli().parse_args(['upload','--video',str(video),'--title','自动化测试 title','--description','第一行\nSecond line','--state-dir',str(root/mode),'--mode',mode,'--audience','not-made-for-kids','--poll-interval','0.1','--step-timeout','8','--upload-timeout','8','--processing-timeout','8','--quiet'])
                return prepare_upload(args)
            for mode in ['draft','publish']:
                state=state_for(mode);workflow=YouTubeWorkflow(client,state,studio=studio,quiet=True)
                try:
                    workflow.run();tabs.append(workflow.tab_id)
                    assert state.data['phase']==('draft_saved' if mode=='draft' else 'published'),state.data
                    counts=client.evaluate(workflow.tab_id,"JSON.parse(localStorage.getItem('studio-fixture'))")
                    assert counts['uploads']==1 and counts['publishes']==(mode=='publish'),counts
                    results.append(mode)
                    if mode=='draft':
                        state.data['config']['mode']='publish';state.save()
                        workflow=YouTubeWorkflow(client,state,studio=studio,quiet=True);workflow.run(True)
                        counts=client.evaluate(workflow.tab_id,"JSON.parse(localStorage.getItem('studio-fixture'))")
                        assert counts['uploads']==1 and counts['publishes']==1,counts
                        results.append('draft_resume_publish')
                finally:
                    if workflow.tab_id and workflow.tab_id not in tabs:tabs.append(workflow.tab_id)
                # Clear local fixture state before next task, no external account involved.
                client.evaluate(workflow.tab_id,"localStorage.clear(); true")
            # Execute commit, lose its HTTP acknowledgement, then reconcile without clicking again.
            state=state_for('publish');state.data['config']['allow_duplicate']=True
            workflow=YouTubeWorkflow(client,state,studio=studio,quiet=True)
            real_action=client.action
            def lose_commit(tab_id,action,**kwargs):
                reply=real_action(tab_id,action,**kwargs)
                if action=='click' and kwargs.get('target')=='#done-button':
                    raise TransportError('simulated lost publish acknowledgement')
                return reply
            client.action=lose_commit
            try:
                workflow.run();raise AssertionError('expected uncertain commit')
            except AutomationError as error:
                assert error.code=='ACTION_OUTCOME_UNKNOWN',error
            finally:
                client.action=real_action;tabs.append(workflow.tab_id)
            assert state.data['publish_intent']
            workflow=YouTubeWorkflow(client,state,studio=studio,quiet=True);workflow.run(True)
            counts=client.evaluate(workflow.tab_id,"JSON.parse(localStorage.getItem('studio-fixture'))")
            assert counts['publishes']==1 and counts['uploads']==1,counts
            results.append('lost_publish_ack_resume')
            # Exercise the production DOM guard against real browser pages.
            for name,page,expected in [
                ('login',b'<input type="email"><p>Sign in</p>','LOGIN_REQUIRED'),
                ('upload_rejected',HTML.replace(b'Upload complete. Processing complete. Checks complete',b'Upload failed'),'YOUTUBE_REJECTED_UPLOAD'),
            ]:
                Handler.page=page
                state=state_for('draft');state.data['config']['state_dir']=str(root/name)
                workflow=YouTubeWorkflow(client,state,studio=studio,quiet=True)
                try:
                    workflow.run();raise AssertionError('expected '+expected)
                except AutomationError as error:
                    assert error.code==expected,(name,error.code,str(error))
                    results.append(name)
                finally:
                    if workflow.tab_id:tabs.append(workflow.tab_id)
                    client.evaluate(workflow.tab_id,'localStorage.clear();true')
        print(json.dumps({'success':True,'passed':results},ensure_ascii=False))
    finally:
        for tab in set(tabs):
            try:client.close_tab(tab)
            except Exception:pass
        server.shutdown();server.server_close()

if __name__=='__main__':main()
