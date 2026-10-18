"""Upload to YouTube Studio through Browser Agent Proxy (no YouTube API key).

Examples (from the repository root; from elsewhere use `python3 /path/to/cli/youtube`):
  python3 -m cli.youtube upload --video /path/video.mp4 --title 'Title' --mode draft
  python3 -m cli.youtube upload --video /path/video.mp4 --title-file title.txt --description-file body.txt --mode publish --audience not-made-for-kids --visibility public
  python3 -m cli.youtube resume --state-file /path/state.json
  python3 -m cli.youtube status --state-file /path/state.json

Progress goes to stderr; one JSON result goes to stdout. A failed upload keeps
its managed Chrome page open. Mutations are not automatically retried. Direct
video download URLs are supported; YouTube watch/share URLs are not video files.
"""
import argparse
import contextlib
import json
import os
from pathlib import Path
import re
import shlex
import sys
import time
from urllib.parse import urlparse
import uuid

from ..common.browser_client import APIError, BASE, BrowserClient, TransportError, load_probe
from ..common.automation import AutomationError, FileLock, TaskState, claim_key
from ..common import media
from ..common.media import fingerprint, log, read_text
from ..common.page_workflow import PageWorkflow, MemoryState, same_text

PROBE = load_probe(Path(__file__).parent / 'probe.js')
STUDIO = 'https://studio.youtube.com'
DEFAULT_STATE_DIR = Path(os.environ.get('XDG_STATE_HOME', str(Path.home() / '.local' / 'state'))) / 'browser-agent' / 'youtube'
AUDIENCES = {'made-for-kids': 'yes', 'not-made-for-kids': 'no'}
READY_PHASES = {'draft_saved', 'published'}
CONTENT_LISTS = {'videos': 'upload', 'shorts': 'short'}
THUMBNAIL_MAX_BYTES = 2 * 1024 * 1024  # Studio's limit for a custom thumbnail


def same_title(a, b):
    return ' '.join((a or '').split()).casefold() == ' '.join((b or '').split()).casefold()


YOUTUBE_HOSTS = ('youtube.com', 'youtu.be')


def download_video(url, directory, **options):
    return media.download_video(url, directory, reject_hosts=YOUTUBE_HOSTS,
                                user_agent='Browser-Agent-YouTube-CLI/1.0', **options)


def validate_state(data):
    config = data['config']
    required = ['title', 'description', 'mode', 'visibility', 'state_dir', 'upload_timeout', 'processing_timeout']
    if any(k not in config for k in required) or config['mode'] not in {'draft', 'publish'} or config['visibility'] not in {'public', 'unlisted', 'private'}:
        raise AutomationError('INVALID_STATE', 'Missing or invalid task configuration', exit_code=2)
    for key in ['upload_timeout', 'processing_timeout']:
        if not isinstance(config[key], (int, float)) or not 0 < config[key] < float('inf'):
            raise AutomationError('INVALID_STATE', f'Invalid {key}', exit_code=2)
    if any(not isinstance(config[k], str) for k in ['title', 'description', 'state_dir']) or config.get('audience') not in {None, 'made-for-kids', 'not-made-for-kids'}:
        raise AutomationError('INVALID_STATE', 'Invalid metadata or audience', exit_code=2)
    video = data.get('video')
    if (not isinstance(video, dict) or not isinstance(video.get('path'), str) or
            not isinstance(video.get('size'), int) or video['size'] <= 0 or
            not isinstance(video.get('mtime_ns'), int) or
            not isinstance(video.get('sha256'), str) or not re.fullmatch('[0-9a-f]{64}', video['sha256'])):
        raise AutomationError('INVALID_STATE', 'Invalid source video fingerprint', exit_code=2)
    if data.get('video_id') and not re.fullmatch(r'[A-Za-z0-9_-]{11}', str(data['video_id'])):
        raise AutomationError('INVALID_STATE', 'Invalid video ID', exit_code=2)


def load_state(path):
    return TaskState.load(path, platform='youtube', validate=validate_state)


def claim_upload(state, registry_directory, *, allow_duplicate=False):
    """Claim this video for this channel before the file is chosen."""
    claim_key(state, registry_directory, state.data['video']['sha256'] + ':' + state.data['channel_id'],
              allow_duplicate=allow_duplicate)
    state.save(upload_intent=True)


def validate_text(title, description):
    if not title.strip() or len(title.encode('utf-16-le')) // 2 > 100:
        raise AutomationError('INVALID_TITLE', '标题不能为空，且不能超过 100 个 UTF-16 字符单位', exit_code=2)
    if len(description.encode('utf-16-le')) // 2 > 5000:
        raise AutomationError('INVALID_DESCRIPTION', '描述不能超过 5000 个 UTF-16 字符单位', exit_code=2)
    if any(char in title + description for char in '<>\x00'):
        raise AutomationError('INVALID_TEXT', '标题和描述不能包含 <、> 或 NUL', exit_code=2)


class YouTubeWorkflow(PageWorkflow):
    def __init__(self, client, state, *, quiet=False, studio=STUDIO, sleep=time.sleep, clock=time.monotonic):
        super().__init__(client, state, PROBE, quiet=quiet, sleep=sleep, clock=clock)
        self.studio = studio.rstrip('/')

    def guard(self, probe):
        if probe.get('challenge'):
            raise AutomationError('VERIFICATION_REQUIRED', 'YouTube/Google 要求验证码或身份验证，请在保留的标签页中处理', exit_code=3)
        if probe.get('loginRequired'):
            raise AutomationError('LOGIN_REQUIRED', 'Chrome 中尚未登录 YouTube；请在保留的标签页登录后 resume', exit_code=3)
        if probe.get('channelSetupRequired'):
            raise AutomationError('CHANNEL_SETUP_REQUIRED', '此账号尚需创建/初始化 YouTube 频道，请先手动完成', exit_code=3)
        if probe.get('accessDenied'):
            raise AutomationError('CHANNEL_ACCESS_DENIED', '当前账号没有访问目标频道的权限', exit_code=3)
        if probe.get('checksIssue'):
            raise AutomationError('CHECKS_REQUIRE_REVIEW', 'YouTube 检查发现版权或限制问题，请人工核验', exit_code=6,
                                  details={'progress': probe.get('progress')})
        host = urlparse(probe.get('url', '')).hostname
        if host and host != urlparse(self.studio).hostname:
            raise AutomationError('UNEXPECTED_ORIGIN', f'页面离开了预期 Studio：{host}', exit_code=3)
        errors = '\n'.join(probe.get('alerts', []) + [probe.get('progress', '')])
        fatal = r'Upload failed|Processing abandoned|Processing failed|Daily upload limit|Video is too long|Unsupported file|Invalid file|上传失败|无法上传|处理失败|每日上传|超出.*上传|不支持.*文件|檔案格式不支援'
        if re.search(fatal, errors, re.I):
            raise AutomationError('YOUTUBE_REJECTED_UPLOAD', errors[:2000], exit_code=6)

    def observe(self, probe):
        ids = probe.get('videoIds') or []
        if len(ids) == 1:
            known = self.state.data.get('video_id')
            if known and known != ids[0]:
                raise AutomationError('VIDEO_MISMATCH', '当前上传窗口的视频 ID 与任务记录不一致', exit_code=7)
            if not known:
                self.state.save(video_id=ids[0], video_url=f'https://youtu.be/{ids[0]}')

    def attach(self, recovering):
        def refuse_if_upload_unknown():
            if recovering and self.state.data.get('upload_intent') and not self.state.data.get('video_id'):
                raise AutomationError('UPLOAD_OUTCOME_UNKNOWN', '原标签已丢失且没有视频 ID，无法确认上次是否创建视频。请先人工核验频道内容列表', exit_code=7)
        self.ensure_tab(platform='youtube', required_actions={'upload_files', 'fill', 'click', 'snapshot', 'set_tab_policy'},
                        min_revision=7, owner_key='youtube_job_id', on_lost=refuse_if_upload_unknown)
        if not recovering or not self.state.data.get('upload_intent'):
            url = self.studio + ('/channel/' + self.config['channel_id'] if self.config.get('channel_id') else '/')
            self.client.navigate(self.tab_id, url)
        self.phase('studio', '检查登录和目标频道')
        if recovering and self.state.data.get('upload_intent') and not self.current_has_wizard():
            self.client.navigate(self.tab_id, self.content_url())
        probe = self.poll(lambda p: p.get('channelId') or p.get('wizard'), self.config['step_timeout'],
                          'STUDIO_NOT_READY', 'Studio 未就绪，可能网络失败、登录失效或界面不受支持')
        channel = probe.get('channelId') or self.state.data.get('channel_id')
        if not channel:
            raise AutomationError('CHANNEL_UNKNOWN', '无法确认当前频道 ID，停止上传', exit_code=3)
        expected = self.config.get('channel_id') or self.state.data.get('channel_id')
        if expected and channel != expected:
            raise AutomationError('CHANNEL_MISMATCH', f'频道不匹配：期望 {expected}，实际 {channel}', exit_code=3)
        self.state.save(channel_id=channel)

    def recent_rows(self, lists=('videos', 'shorts'), limit=30, settle=20):
        """Newest rows of Studio's content lists (sorted by date, first page only)."""
        rows = []
        for name in lists:
            self.client.navigate(self.tab_id, self.studio + f"/channel/{self.state.data['channel_id']}/videos/{CONTENT_LISTS[name]}")
            try:
                # Wait for this list's own tab so rows of the previous list are not read.
                probe = self.poll(lambda p: p.get('contentList') == name and p.get('rows'), settle,
                                  'CONTENT_LIST_EMPTY', '')
            except AutomationError as error:
                if error.code != 'CONTENT_LIST_EMPTY':
                    raise
                continue
            for row in probe['rows'][:limit]:
                rows.append({'video_id': row['videoIds'][0], 'title': row.get('title'),
                             'visibility': 'draft' if row.get('draft') else row.get('visibility'),
                             'date': row.get('date'), 'list': name,
                             'url': f"https://youtu.be/{row['videoIds'][0]}"})
        return rows

    def check_title_unused(self):
        self.phase('duplicate_check', '按标题检查频道最近的视频与 Shorts，避免重复发表')
        matches = [r for r in self.recent_rows(limit=50) if same_title(r['title'], self.config['title'])]
        if matches:
            raise AutomationError('DUPLICATE_TITLE', '频道最近内容中已有同名视频（含草稿）；确认要再传请加 --allow-duplicate',
                                  exit_code=7, details={'matches': matches})

    def current_has_wizard(self):
        try:
            return self.probe().get('wizard', False)
        except AutomationError as error:
            if error.code in {'LOGIN_REQUIRED', 'VERIFICATION_REQUIRED', 'CHANNEL_ACCESS_DENIED'}:
                raise
            return False

    def content_url(self):
        return self.studio + f"/channel/{self.state.data['channel_id']}/videos/upload"

    def find_row(self, probe):
        video_id = self.state.data.get('video_id')
        return next((r for r in probe.get('rows', []) if video_id in r.get('videoIds', [])), None)

    def load_content_row(self):
        self.client.navigate(self.tab_id, self.content_url())
        try:
            return self.poll(lambda p: self.find_row(p), self.config['step_timeout'],
                             'EXISTING_VIDEO_NOT_FOUND', '当前内容列表没有找到记录的视频 ID')
        except AutomationError as error:
            if error.code != 'EXISTING_VIDEO_NOT_FOUND':
                raise
            # A short video can be classified into the separate Shorts tab.
            # Switch only through an actually observed tab, then verify the same ID.
            tab = (self.last_probe or {}).get('contentTabs', {}).get('shorts')
            if not tab or not tab.get('enabled'):
                raise
            self.mutate('inspect_shorts_tab', 'click', target=tab['target'])
            return self.poll(lambda p: self.find_row(p), self.config['step_timeout'],
                             'EXISTING_VIDEO_NOT_FOUND', '视频与 Shorts 列表均未找到记录的视频 ID，请人工核验')

    def reopen_existing(self):
        if not self.state.data.get('video_id'):
            raise AutomationError('UPLOAD_OUTCOME_UNKNOWN', '没有可用于恢复的视频 ID', exit_code=7)
        probe = self.load_content_row()
        row = self.find_row(probe)
        if not row.get('draft'):
            if self.state.data.get('publish_intent') and self.config['mode'] == 'publish' and row.get('visibility') == self.config['visibility'] and row.get('title') == self.config['title']:
                self.state.save(phase='published', status='completed', evidence={'row': row, 'reconciled': True})
                return
            raise AutomationError('EXISTING_VIDEO_NOT_DRAFT', '原视频已不是草稿，拒绝重放上传/发布；请核验现有视频状态', exit_code=7,
                                  details={'row': row})
        target = row.get('editDraft')
        if not target:
            raise AutomationError('DRAFT_EDITOR_UNAVAILABLE', '找到了草稿，但没有可识别的编辑草稿按钮', details={'row': row})
        self.mutate('open_existing_draft', 'click', target=target['target'])
        self.poll(lambda p: p.get('wizard') and p.get('title'), self.config['step_timeout'],
                  'DRAFT_EDITOR_UNAVAILABLE', '无法重新打开原草稿编辑器')

    def open_upload(self):
        self.phase('open_upload', '打开上传窗口')
        probe = self.probe()
        if probe.get('wizard') and probe.get('fileInput'):
            return
        if not probe.get('actions', {}).get('openUpload'):
            self.click('create')
        self.click('openUpload')
        self.poll(lambda p: p.get('wizard') and p.get('fileInput'), self.config['step_timeout'],
                  'UPLOAD_DIALOG_UNAVAILABLE', '上传窗口没有出现，可能需要先完成频道初始化')

    def select_video(self):
        self.phase('select_video', '选择本地视频文件（此动作只执行一次）')
        video = self.state.data['video']
        path = Path(video['path'])
        if not path.is_file() or path.stat().st_size != video['size'] or path.stat().st_mtime_ns != video['mtime_ns']:
            raise AutomationError('VIDEO_CHANGED', '视频文件在准备后发生变化或已丢失，请重新确认素材', exit_code=2)
        probe = self.probe()
        if not probe.get('fileInput'):
            raise AutomationError('FILE_INPUT_UNAVAILABLE', '没有找到上传视频的文件输入框')
        claim_upload(self.state, self.config['state_dir'], allow_duplicate=self.config.get('allow_duplicate', False))
        selected = self.mutate('select_video', 'upload_files', target=probe['fileInput']['target'], files=[video['path']])
        entries = selected.get('files', [])
        if len(entries) != 1 or entries[0].get('size') != video['size'] or entries[0].get('name') != path.name:
            raise AutomationError('FILE_SELECTION_MISMATCH', 'Chrome 选中的文件名/大小与请求不一致', exit_code=7)
        self.state.save(file_selected=True)
        self.poll(lambda p: p.get('title') and p['title']['enabled'], self.config['step_timeout'],
                  'METADATA_EDITOR_UNAVAILABLE', '选择文件后没有出现视频信息表单')

    def fill_metadata(self):
        self.phase('metadata', '填写并核验标题、描述及受众')
        for key in ['title', 'description']:
            probe = self.poll(lambda p: p.get(key), self.config['step_timeout'], 'FIELD_UNAVAILABLE', f'未找到 {key} 输入框')
            field = probe[key]
            value = self.config[key]
            if not same_text(field['value'], value):
                self.mutate('fill_' + key, 'fill', target=field['target'], value=value)
            verified = self.probe().get(key)
            if not verified or verified.get('invalid') or not same_text(verified['value'], value):
                raise AutomationError('METADATA_VERIFICATION_FAILED', f'{key} 填写后核验失败')
        if self.config.get('thumbnail'):
            self.set_thumbnail()
        audience = self.config.get('audience')
        if audience:
            probe = self.poll(lambda p: p.get('audience', {}).get(AUDIENCES[audience]), self.config['step_timeout'],
                              'AUDIENCE_UNAVAILABLE', '找不到“是否面向儿童”的选项')
            radio = probe['audience'][AUDIENCES[audience]]
            if not radio['selected']:
                self.mutate('set_audience', 'click', target=radio['target'])
            self.poll(lambda p: (p.get('audience', {}).get(AUDIENCES[audience]) or {}).get('selected'),
                      self.config['step_timeout'], 'AUDIENCE_VERIFICATION_FAILED', '受众设置未生效')
        self.state.save(metadata_verified=True)

    def set_thumbnail(self):
        """Choose the custom thumbnail in the open details form (upload wizard or
        a video's edit page). Re-choosing the same image is harmless, so a resume
        only skips it when the page already shows a custom one."""
        path = Path(self.config['thumbnail'])
        probe = self.poll(lambda p: p.get('thumbnail'), self.config['step_timeout'],
                          'THUMBNAIL_UNAVAILABLE', '找不到缩略图上传控件')
        thumb = probe['thumbnail']
        if self.state.data.get('thumbnail_set') and thumb.get('custom'):
            return
        if not thumb.get('enabled'):
            raise AutomationError('THUMBNAIL_NOT_ALLOWED', '此频道当前不能上传自定义缩略图（可能需要先验证频道/手机号）',
                                  exit_code=6, details={'text': thumb.get('text')})
        if not path.is_file():
            raise AutomationError('INVALID_IMAGE', f'缩略图文件不存在：{path}', exit_code=2)
        selected = self.mutate('select_thumbnail', 'upload_files', target=thumb['target'], files=[str(path)])
        entries = selected.get('files', [])
        if len(entries) != 1 or entries[0].get('size') != path.stat().st_size:
            raise AutomationError('FILE_SELECTION_MISMATCH', 'Chrome 选中的缩略图与请求不一致', exit_code=7)
        self.poll(lambda p: (p.get('thumbnail') or {}).get('custom'), self.config['step_timeout'],
                  'THUMBNAIL_UNVERIFIED', '选择缩略图后页面没有显示自定义缩略图（格式/尺寸被拒？）')
        self.state.save(thumbnail_set=True)

    def edit_thumbnail(self, video_id):
        """Replace an existing video's thumbnail on its edit page, save, and
        verify after reloading."""
        url = self.studio + f'/video/{video_id}/edit'
        self.phase('edit', f'打开 {video_id} 的编辑页')
        self.client.navigate(self.tab_id, url)
        self.poll(lambda p: p.get('title') and p.get('thumbnail'), self.config['step_timeout'],
                  'EDITOR_UNAVAILABLE', '视频编辑页没有就绪（视频 ID 不属于此频道？）')
        self.phase('thumbnail', '上传缩略图')
        self.set_thumbnail()
        self.phase('saving', '保存并重新载入核验')
        probe = self.poll(lambda p: (p.get('editSave') or {}).get('enabled'), self.config['step_timeout'],
                          'SAVE_UNAVAILABLE', '保存按钮不可点')
        self.mutate('save_edit', 'click', target=probe['editSave']['target'])
        self.poll(lambda p: p.get('editSave') and not p['editSave']['enabled'] and not p.get('saving'),
                  self.config['step_timeout'], 'SAVE_UNVERIFIED', '点击保存后没有确认保存完成')
        self.client.navigate(self.tab_id, url)
        probe = self.poll(lambda p: p.get('thumbnail'), self.config['step_timeout'],
                          'EDITOR_UNAVAILABLE', '重新载入后编辑页没有就绪')
        if not probe['thumbnail'].get('custom'):
            raise AutomationError('THUMBNAIL_NOT_PERSISTED', '重新载入后没有看到自定义缩略图，请人工核验', exit_code=7)
        self.state.save(phase='thumbnail_saved', status='completed', evidence={'video_id': video_id, 'custom_thumbnail': True})

    def wait_upload(self):
        self.phase('uploading', '等待上传完成；失败或超时会保留页面')
        self.poll(lambda p: p.get('uploadComplete') or self.state.data.get('upload_complete'), self.config['upload_timeout'],
                  'UPLOAD_TIMEOUT', '未在时限内确认上传完成，请检查网络、上传进度和账号限制')
        self.state.save(upload_complete=True)
        if not self.state.data.get('video_id'):
            raise AutomationError('VIDEO_ID_UNKNOWN', '上传后没有识别出唯一视频 ID，停止关闭/发布以免误操作', exit_code=7)

    def navigate_visibility(self):
        self.phase('visibility', '进入可见性步骤')
        for _ in range(6):
            probe = self.probe()
            if probe.get('privacy', {}).get(self.config['visibility']):
                return
            if probe.get('checksIssue'):
                raise AutomationError('CHECKS_REQUIRE_REVIEW', '发布检查需要人工处理', exit_code=6)
            next_button = probe.get('actions', {}).get('next')
            if not next_button or not next_button.get('enabled'):
                raise AutomationError('PUBLISH_BLOCKED', '下一步不可操作；请检查必填项、变现/广告设置或平台提示',
                                      details={'alerts': probe.get('alerts'), 'progress': probe.get('progress')})
            before = probe.get('step')
            if not before:
                raise AutomationError('STEP_UNKNOWN', '无法识别上传向导当前步骤，停止继续以免重复点击')
            self.mutate('next_step', 'click', target=next_button['target'])
            def transitioned(p):
                if p.get('alerts'):
                    raise AutomationError('PUBLISH_BLOCKED', '步骤有校验错误，停止继续', details={'alerts': p['alerts']})
                return (p.get('step') and p['step'] != before) or p.get('privacy', {}).get(self.config['visibility'])
            self.poll(transitioned, self.config['step_timeout'], 'STEP_TRANSITION_TIMEOUT',
                      '点击下一步后没有确认步骤切换；保留页面，禁止重复点击')
        raise AutomationError('VISIBILITY_UNAVAILABLE', '没有找到可见性步骤，界面可能包含额外的必需配置')

    def verify_saved_metadata(self):
        probe = self.probe()
        for key in ['title', 'description']:
            field = probe.get(key)
            if not field or not same_text(field['value'], self.config[key]):
                raise AutomationError('SAVED_METADATA_MISMATCH', f'重新打开后 {key} 与请求不一致', exit_code=7)

    def save_draft(self):
        self.phase('saving_draft', '关闭上传向导保存为草稿，并按视频 ID 重新核验')
        self.poll(lambda p: not p.get('saving'), self.config['step_timeout'], 'AUTOSAVE_TIMEOUT', '元数据仍在保存中')
        self.click('close')
        self.poll(lambda p: not p.get('wizard'), self.config['step_timeout'], 'DRAFT_CLOSE_TIMEOUT', '草稿窗口没有关闭')
        self.load_content_row()
        probe = self.poll(lambda p: (self.find_row(p) or {}).get('draft'), self.config['step_timeout'],
                          'DRAFT_SAVE_UNVERIFIED', '没有在内容列表中核验到此视频 ID 的草稿状态')
        row = self.find_row(probe)
        self.state.save(draft_row_evidence=row)
        self.reopen_existing()
        self.verify_saved_metadata()
        # Leave a verified draft; do not click the wizard's Save/Publish button.
        self.click('close')
        self.poll(lambda p: not p.get('wizard'), self.config['step_timeout'], 'DRAFT_CLOSE_TIMEOUT', '核验后未能关闭草稿窗口')
        self.state.save(phase='draft_saved', status='completed', evidence={'video_id': self.state.data['video_id'],
                        'draft_row': row, 'metadata_reopened_and_verified': True})

    def publish(self):
        self.navigate_visibility()
        wait_checks = self.config.get('wait_checks', False)
        # Studio lets a video be published while its checks still run (they
        # continue afterwards); by default only an issue already found stops us.
        self.phase('processing', '等待发布就绪' + ('及检查完成' if wait_checks else '（检查可在发布后继续）') + '；不要求高清转码完成')
        probe = self.poll(lambda p: (not wait_checks or (p.get('checksComplete') and not p.get('checksBusy'))) and
                          (p.get('processingComplete') or
                           ((p.get('actions', {}).get('done') or {}).get('enabled') and
                            p.get('privacy', {}).get(self.config['visibility']))),
                          self.config['processing_timeout'], 'PROCESSING_TIMEOUT',
                          '未确认处理/检查及发布就绪状态；保留页面等待，可能需要人工检查')
        self.state.save(processing_complete=bool(probe.get('processingComplete')), publish_ready=True,
                        checks_complete_at_publish=bool(probe.get('checksComplete')),
                        readiness_evidence={'progress': probe.get('progress'), 'done': probe.get('actions', {}).get('done')})
        probe = self.probe()
        radio = probe['privacy'][self.config['visibility']]
        if not radio['selected']:
            self.mutate('set_visibility', 'click', target=radio['target'])
        self.poll(lambda p: (p.get('privacy', {}).get(self.config['visibility']) or {}).get('selected'),
                  self.config['step_timeout'], 'VISIBILITY_VERIFICATION_FAILED', '可见性选择没有生效')
        probe = self.probe()
        if probe.get('checksIssue') or (wait_checks and probe.get('checksBusy')):
            raise AutomationError('CHECKS_REQUIRE_REVIEW', '检查未完成或有需要人工处理的限制，停止发布', exit_code=6)
        done = probe.get('actions', {}).get('done')
        if not done or not done['enabled']:
            raise AutomationError('PUBLISH_BLOCKED', '发布/保存按钮不可操作', details={'alerts': probe.get('alerts')})
        # Durable intent precedes the single commit. Resume reconciles the content
        # row if this command's reply is lost; it never blindly presses it twice.
        self.phase('publishing', '提交发布（只提交一次），等待平台结果')
        self.state.save(publish_intent=True)
        self.mutate('publish', 'click', target=done['target'])
        probe = self.poll(lambda p: p.get('publicationConfirmed') and self.state.data['video_id'] in p.get('videoIds', []),
                          self.config['step_timeout'], 'PUBLISH_OUTCOME_UNKNOWN', '发布命令已发送，但没有核验到此视频的完成确认；请 resume 核对现有内容')
        confirmation = probe['confirmationText']
        self.load_content_row()
        probe = self.poll(lambda p: (self.find_row(p) or {}).get('visibility') == self.config['visibility'] and
                          not (self.find_row(p) or {}).get('draft'), self.config['step_timeout'],
                          'PUBLISH_OUTCOME_UNKNOWN', '没有在内容列表中核验到请求的可见性；请人工检查或 resume')
        row = self.find_row(probe)
        if row.get('title') != self.config['title']:
            raise AutomationError('PUBLISHED_TITLE_MISMATCH', '已发布条目的标题与请求不一致，请人工核验', exit_code=7)
        self.state.save(phase='published', status='completed', evidence={'confirmation': confirmation, 'row': row,
                        'visibility': self.config['visibility'], 'video_id': self.state.data['video_id']})

    def run(self, recovering=False):
        self.attach(recovering)
        if self.state.data.get('status') == 'completed' and self.state.data.get('phase') == 'published':
            return
        if recovering and self.state.data.get('publish_intent'):
            # Reconcile an uncertain commit; never re-enter publish automatically.
            probe = self.load_content_row()
            row = self.find_row(probe)
            if row.get('draft') or row.get('visibility') != self.config['visibility']:
                raise AutomationError('PUBLISH_OUTCOME_UNKNOWN', '发布结果尚不能确认；原任务不会自动再次点击发布', exit_code=7, details={'row': row})
            if row.get('title') != self.config['title']:
                raise AutomationError('PUBLISHED_TITLE_MISMATCH', '原发布条目标题不一致', exit_code=7)
            self.state.save(phase='published', status='completed', evidence={'row': row, 'reconciled': True})
            return
        if not self.state.data.get('upload_intent'):
            if not self.config.get('allow_duplicate'):
                self.check_title_unused()
            self.open_upload()
            self.select_video()
        else:
            probe = self.probe()
            if not probe.get('wizard') or not probe.get('title'):
                if self.state.data.get('video_id'):
                    self.reopen_existing()
                else:
                    raise AutomationError('UPLOAD_OUTCOME_UNKNOWN', '上次文件选择可能已生效，但无法定位原上传窗口；禁止重新选择视频', exit_code=7)
        self.poll(lambda p: len(p.get('videoIds', [])) == 1, self.config['step_timeout'],
                  'VIDEO_ID_UNKNOWN', '无法确认上传窗口的唯一视频 ID，停止修改/发布')
        if not self.probe().get('title'):
            self.click('details')
        self.fill_metadata()
        self.wait_upload()
        if self.config['mode'] == 'draft':
            self.save_draft()
        else:
            self.publish()


def add_common(parser):
    parser.add_argument('--api-url', '--base', default=None, help='Browser Agent Go API，默认 http://127.0.0.1:12345')
    parser.add_argument('--state-dir', type=Path, default=DEFAULT_STATE_DIR, help='任务与上传去重索引目录')
    parser.add_argument('--state-file', type=Path, help='任务状态 JSON 路径；resume/status 必填')
    parser.add_argument('--quiet', action='store_true', help='不输出 stderr 进度')
    parser.add_argument('--json', action='store_true', help='最终 JSON 使用单行格式')
    parser.add_argument('--keep-tab', action='store_true', help='成功后也保留受管页面；失败始终保留')


def parser_for_cli():
    parser = argparse.ArgumentParser(prog='cli.youtube', description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = parser.add_subparsers(dest='command', required=True)
    upload = sub.add_parser('upload', help='上传视频并保存草稿或立即发布')
    add_common(upload)
    video = upload.add_mutually_exclusive_group(required=True)
    video.add_argument('--video', '--video-file', help='本地视频文件路径')
    video.add_argument('--video-url', help='HTTP(S) 视频文件直链，不支持播放页 URL')
    title = upload.add_mutually_exclusive_group(required=True)
    title.add_argument('--title', help='视频标题')
    title.add_argument('--title-file', help='UTF-8 标题文件')
    desc = upload.add_mutually_exclusive_group()
    desc.add_argument('--description', help='视频描述，默认空字符串')
    desc.add_argument('--description-file', help='UTF-8 描述文件')
    upload.add_argument('--mode', choices=['draft', 'publish'], default='draft', help='draft 保存草稿（默认）；publish 立即提交所选可见性')
    upload.add_argument('--visibility', choices=['public', 'unlisted', 'private'], help='publish 模式默认 public；draft 不设置可见性')
    upload.add_argument('--audience', choices=list(AUDIENCES), help='是否面向儿童；publish 模式必须显式指定')
    upload.add_argument('--channel-id', help='预期 UC... 频道 ID，防止上传到错误频道')
    upload.add_argument('--upload-timeout', type=float, default=1800, help='等待上传完成的秒数，默认 1800')
    upload.add_argument('--processing-timeout', type=float, default=1800, help='等待发布就绪（及 --wait-checks 时检查完成）的秒数，默认 1800')
    upload.add_argument('--wait-checks', action='store_true', help='等 YouTube 版权/限制检查完成后再发布；默认检查在发布后继续，只有已发现问题才停止')
    upload.add_argument('--step-timeout', type=float, default=60, help='每个页面步骤/核验超时秒数，默认 60')
    upload.add_argument('--poll-interval', type=float, default=2, help='只读状态检查间隔秒数，默认 2')
    upload.add_argument('--download-timeout', type=float, default=120, help='远程视频下载 socket 超时秒数')
    upload.add_argument('--max-download-mb', type=int, default=20480, help='直链下载大小上限 MiB，默认 20480')
    thumb = upload.add_mutually_exclusive_group()
    thumb.add_argument('--thumbnail', help='自定义缩略图（jpg/png ≤2MB 原样上传，其他格式或过大时用 ffmpeg 转为 jpg）')
    thumb.add_argument('--thumbnail-at', type=float, help='从视频第 N 秒截一帧做缩略图（需要 ffmpeg）')
    upload.add_argument('--allow-duplicate', action='store_true', help='明确允许对同一频道重复上传同内容；不会自动重试原任务')
    resume = sub.add_parser('resume', help='从状态文件恢复；已选择视频不会重新选择，未知发布只核验结果')
    add_common(resume)
    resume.add_argument('--mode', choices=['draft', 'publish'], help='可将已保存的草稿改为发布')
    resume.add_argument('--audience', choices=list(AUDIENCES), help='补充/更新受众选择')
    resume.add_argument('--visibility', choices=['public', 'unlisted', 'private'], help='恢复发布的目标可见性')
    status = sub.add_parser('status', help='读取本地任务状态，不连接浏览器')
    add_common(status)
    thumbnail = sub.add_parser('thumbnail', help='更换已上传视频的缩略图：编辑页上传、保存并重新载入核验')
    thumbnail.add_argument('--api-url', '--base', default=None, help='Browser Agent Go API，默认 http://127.0.0.1:12345')
    thumbnail.add_argument('--video-id', required=True, help='11 位视频 ID（youtu.be/<id>）')
    thumbnail.add_argument('--channel-id', help='预期 UC... 频道 ID')
    source = thumbnail.add_mutually_exclusive_group(required=True)
    source.add_argument('--thumbnail', help='缩略图图片')
    source.add_argument('--thumbnail-at', type=float, help='从 --video 第 N 秒截一帧（需要 ffmpeg）')
    thumbnail.add_argument('--video', help='与 --thumbnail-at 一起使用的本地视频文件')
    thumbnail.add_argument('--step-timeout', type=float, default=60, help='每个页面步骤超时秒数，默认 60')
    thumbnail.add_argument('--poll-interval', type=float, default=2, help='只读状态检查间隔秒数，默认 2')
    thumbnail.add_argument('--keep-tab', action='store_true', help='结束后保留页面（失败时总是保留）')
    thumbnail.add_argument('--quiet', action='store_true', help='不输出 stderr 进度')
    thumbnail.add_argument('--json', action='store_true', help='最终 JSON 使用单行格式')
    recent = sub.add_parser('recent', help='列出频道最近的视频/Shorts（标题、ID、可见性、日期），用于发布前查重')
    recent.add_argument('--api-url', '--base', default=None, help='Browser Agent Go API，默认 http://127.0.0.1:12345')
    recent.add_argument('--channel-id', help='UC... 频道 ID；默认当前 Studio 频道')
    recent.add_argument('--list', choices=['all', 'videos', 'shorts'], default='all', help='读取哪个内容列表，默认两个都读')
    recent.add_argument('--limit', type=int, default=30, help='每个列表最多返回条数（仅首屏），默认 30')
    recent.add_argument('--title', help='检查是否已有同名视频（忽略大小写与多余空白）；结果含 title_matches')
    recent.add_argument('--step-timeout', type=float, default=60, help='页面就绪超时秒数，默认 60')
    recent.add_argument('--poll-interval', type=float, default=2, help='只读状态检查间隔秒数，默认 2')
    recent.add_argument('--quiet', action='store_true', help='不输出 stderr 进度')
    recent.add_argument('--json', action='store_true', help='最终 JSON 使用单行格式')
    return parser


def prepare_thumbnail(image, at, video, directory):
    """Local path of an upload-ready thumbnail, or None when none was asked for."""
    if at is not None:
        if not video:
            raise AutomationError('INVALID_ARGUMENT', '--thumbnail-at 需要视频文件（--video）', exit_code=2)
        if at < 0:
            raise AutomationError('INVALID_ARGUMENT', '--thumbnail-at 不能为负数', exit_code=2)
        image = media.extract_frame(video, at, Path(directory) / f'thumbnail-{at:g}s.jpg')
    if image is None:
        return None
    return str(media.prepare_image(image, directory, max_bytes=THUMBNAIL_MAX_BYTES))


def run_thumbnail(args):
    if not re.fullmatch(r'[A-Za-z0-9_-]{11}', args.video_id):
        raise AutomationError('INVALID_ARGUMENT', '--video-id 必须是 11 位视频 ID', exit_code=2)
    if args.channel_id and not re.fullmatch(r'UC[A-Za-z0-9_-]{22}', args.channel_id):
        raise AutomationError('INVALID_CHANNEL', '--channel-id 必须是 UC 开头的 24 位频道 ID', exit_code=2)
    job_id = 'thumbnail-' + uuid.uuid4().hex
    work = DEFAULT_STATE_DIR / job_id
    thumbnail = prepare_thumbnail(args.thumbnail, args.thumbnail_at, args.video, work)
    state = MemoryState({'job_id': job_id, 'video_id': args.video_id,
                         'config': {'channel_id': args.channel_id, 'step_timeout': args.step_timeout,
                                    'poll_interval': args.poll_interval, 'thumbnail': thumbnail}})
    client = BrowserClient(args.api_url or BASE)
    workflow = YouTubeWorkflow(client, state, quiet=args.quiet)
    ok = False
    try:
        workflow.attach(recovering=False)
        workflow.edit_thumbnail(args.video_id)
        ok = True
    finally:
        if workflow.tab_id and (ok and not args.keep_tab):
            with contextlib.suppress(Exception):
                client.close_tab(workflow.tab_id)
    return {'success': True, 'video_id': args.video_id, 'channel_id': state.data.get('channel_id'),
            'thumbnail': thumbnail, 'evidence': state.data.get('evidence')}


def run_recent(args):
    if args.channel_id and not re.fullmatch(r'UC[A-Za-z0-9_-]{22}', args.channel_id):
        raise AutomationError('INVALID_CHANNEL', '--channel-id 必须是 UC 开头的 24 位频道 ID', exit_code=2)
    if args.limit <= 0 or args.step_timeout <= 0 or args.poll_interval <= 0:
        raise AutomationError('INVALID_ARGUMENT', '--limit/--step-timeout/--poll-interval 必须是正数', exit_code=2)
    state = MemoryState({'job_id': 'recent-' + uuid.uuid4().hex,
                           'config': {'channel_id': args.channel_id, 'step_timeout': args.step_timeout,
                                      'poll_interval': args.poll_interval}})
    client = BrowserClient(args.api_url or BASE)
    workflow = YouTubeWorkflow(client, state, quiet=args.quiet)
    try:
        workflow.attach(recovering=False)
        lists = ('videos', 'shorts') if args.list == 'all' else (args.list,)
        log('读取内容列表：' + ', '.join(lists), args.quiet)
        rows = workflow.recent_rows(lists, limit=args.limit)
    finally:
        if workflow.tab_id:
            with contextlib.suppress(Exception):
                client.close_tab(workflow.tab_id)
    result = {'success': True, 'channel_id': state.data.get('channel_id'), 'videos': rows,
              'note': '每个列表只读首屏、按日期倒序；更早的视频不在结果中'}
    if args.title is not None:
        result['title'] = args.title
        result['title_matches'] = [r for r in rows if same_title(r['title'], args.title)]
        result['duplicate'] = bool(result['title_matches'])
    return result


def summarize(state, *, success=None, error=None):
    data = state.data if state else {}
    result = {'success': success if success is not None else data.get('status') == 'completed',
              'status': data.get('status'), 'stage': data.get('phase'), 'job_id': data.get('job_id'),
              'state_file': str(state.path) if state else None, 'tab_id': data.get('tab_id'),
              'channel_id': data.get('channel_id'), 'video_id': data.get('video_id'),
              'video_url': data.get('video_url'), 'mode': data.get('config', {}).get('mode'),
              'evidence': data.get('evidence')}
    if error:
        result.update(code=error.code, message=str(error), details=error.details)
    elif data.get('error'):
        result['last_error'] = data['error']
    if state:
        result['resume_command'] = shlex.join([sys.executable, str(Path(__file__).resolve().parent), 'resume', '--state-file', str(state.path), '--api-url', data.get('api_url', BASE)])
    return result


def prepare_upload(args):
    title = read_text(args.title, args.title_file).strip()
    description = read_text(args.description, args.description_file)
    validate_text(title, description)
    if args.mode == 'publish' and not args.audience:
        raise AutomationError('AUDIENCE_REQUIRED', 'publish 模式必须指定 --audience made-for-kids 或 not-made-for-kids', exit_code=2)
    if args.mode == 'draft' and args.visibility:
        raise AutomationError('INVALID_ARGUMENT', '--visibility 仅用于 publish，草稿模式不会点击发布/保存可见性', exit_code=2)
    if args.channel_id and not re.fullmatch(r'UC[A-Za-z0-9_-]{22}', args.channel_id):
        raise AutomationError('INVALID_CHANNEL', '--channel-id 必须是 UC 开头的 24 位频道 ID', exit_code=2)
    for name in ['upload_timeout', 'processing_timeout', 'step_timeout', 'poll_interval', 'download_timeout', 'max_download_mb']:
        value = getattr(args, name)
        if value <= 0 or value == float('inf') or value != value:
            raise AutomationError('INVALID_ARGUMENT', f'--{name.replace("_", "-")} 必须是有限正数', exit_code=2)
    root = args.state_dir.expanduser().resolve()
    job_id = uuid.uuid4().hex
    state_path = args.state_file.expanduser().resolve() if args.state_file else root / job_id / 'state.json'
    if state_path.exists():
        raise AutomationError('STATE_EXISTS', '状态文件已存在，请使用 resume 或指定新路径', exit_code=2)
    video_path = args.video or download_video(args.video_url, state_path.parent / ('media-' + job_id),
                        max_mb=args.max_download_mb, timeout=args.download_timeout, quiet=args.quiet)
    video = fingerprint(video_path, args.quiet, code='INVALID_VIDEO')
    thumbnail = prepare_thumbnail(args.thumbnail, args.thumbnail_at, video['path'], state_path.parent / ('media-' + job_id))
    config = {name: getattr(args, name) for name in ['mode', 'audience', 'channel_id', 'upload_timeout',
              'processing_timeout', 'step_timeout', 'poll_interval', 'allow_duplicate', 'wait_checks']}
    config.update(title=title, description=description, visibility=args.visibility or 'public', state_dir=str(root),
                  thumbnail=thumbnail)
    return TaskState(state_path, {'schema_version': 1, 'platform': 'youtube', 'job_id': job_id,
                     'created_at': time.time(), 'phase': 'prepared', 'status': 'prepared', 'config': config,
                     'api_url': args.api_url or BASE, 'video': video})


def main(argv=None):
    parser = parser_for_cli()
    args = parser.parse_args(argv)
    state = workflow = None
    result = None
    task_lock = None
    state_writable = False
    exit_code = 0
    try:
        if args.command in {'recent', 'thumbnail'}:
            result = (run_recent if args.command == 'recent' else run_thumbnail)(args)
            print(json.dumps(result, ensure_ascii=False, indent=None if args.json else 2))
            return 0
        if args.command in {'resume', 'status'}:
            if not args.state_file:
                raise AutomationError('STATE_REQUIRED', '--state-file 为必填项', exit_code=2)
            state = load_state(args.state_file)
            if args.command == 'status':
                result = summarize(state)
                print(json.dumps(result, ensure_ascii=False, indent=None if args.json else 2))
                return 0
        else:
            state = prepare_upload(args)
        task_lock = FileLock(str(state.path) + '.lock')
        task_lock.__enter__()
        if args.command == 'resume':
            state = load_state(args.state_file)
        elif state.path.exists():
            raise AutomationError('STATE_EXISTS', '状态文件已存在，请使用 resume', exit_code=2)
        recovering = args.command == 'resume'
        if recovering:
            config = state.data['config']
            for key in ['mode', 'audience', 'visibility']:
                value = getattr(args, key, None)
                if value is not None:
                    if state.data.get('publish_intent') and value != config.get(key):
                        raise AutomationError('COMMIT_ALREADY_SENT', '发布已提交或结果未知，不能在恢复时修改发布参数', exit_code=7)
                    config[key] = value
            if config['mode'] == 'publish' and not config.get('audience'):
                raise AutomationError('AUDIENCE_REQUIRED', '恢复发布仍需显式指定 --audience', exit_code=2)
            if state.data.get('status') == 'completed' and state.data.get('phase') == 'published':
                result = summarize(state)
                print(json.dumps(result, ensure_ascii=False, indent=None if args.json else 2))
                return 0
            if state.data.get('phase') == 'draft_saved' and config['mode'] == 'draft':
                result = summarize(state)
                print(json.dumps(result, ensure_ascii=False, indent=None if args.json else 2))
                return 0
        state_writable = True
        state.save(status='running', error=None, api_url=args.api_url or state.data.get('api_url') or BASE)
        workflow = YouTubeWorkflow(BrowserClient(args.api_url or state.data.get('api_url') or BASE), state, quiet=args.quiet)
        workflow.run(recovering=recovering)
        result = summarize(state)
        if not args.keep_tab:
            try:
                workflow.client.close_tab(workflow.tab_id)
                state.save(tab_closed=True)
            except Exception as cleanup_error:
                result['cleanup_warning'] = str(cleanup_error)
    except KeyboardInterrupt:
        error = AutomationError('INTERRUPTED', '用户中止；保留页面和任务，恢复前核验已有上传/发布', exit_code=130)
        exit_code = 130
        if state_writable:
            with contextlib.suppress(Exception):
                state.save(status='interrupted', error={'code': error.code, 'message': str(error)})
        result = summarize(state, success=False, error=error)
    except Exception as caught:
        if isinstance(caught, AutomationError):
            error = caught
        elif isinstance(caught, (APIError, TransportError)):
            error = AutomationError('BROWSER_API_FAILED', str(caught), exit_code=7 if state and state.data.get('upload_intent') else 3)
        else:
            error = AutomationError('UNEXPECTED_ERROR', f'{type(caught).__name__}: {caught}', exit_code=4)
        exit_code = error.exit_code
        if state_writable:
            with contextlib.suppress(Exception):
                state.save(status='failed', error={'code': error.code, 'message': str(error), 'details': error.details})
            if workflow and error.code == 'DUPLICATE_TITLE' and not state.data.get('upload_intent'):
                # Nothing was uploaded, so there is no page worth keeping.
                with contextlib.suppress(Exception):
                    workflow.client.close_tab(workflow.tab_id)
                    state.save(tab_closed=True)
            elif workflow:
                with contextlib.suppress(Exception):
                    workflow.diagnostics()
        result = summarize(state, success=False, error=error)
    finally:
        if task_lock and task_lock.stream and not task_lock.stream.closed:
            task_lock.__exit__()
    print(json.dumps(result, ensure_ascii=False, indent=None if args.json else 2))
    return exit_code


if __name__ == '__main__':
    raise SystemExit(main())
