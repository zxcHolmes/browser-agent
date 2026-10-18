"""Reusable task state, locking, and stage-aware failures for browser CLIs."""
import contextlib
import json
import os
import re
from pathlib import Path
import tempfile
import time


class AutomationError(RuntimeError):
    def __init__(self, code, message, *, exit_code=4, details=None):
        super().__init__(message)
        self.code, self.exit_code, self.details = code, exit_code, details or {}


def atomic_json(path, value):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    temp = None
    try:
        with tempfile.NamedTemporaryFile(mode='w', encoding='utf-8', dir=path.parent,
                                         prefix=path.name + '.', suffix='.tmp', delete=False) as stream:
            temp = Path(stream.name)
            os.chmod(temp, 0o600)
            json.dump(value, stream, ensure_ascii=False, indent=2)
            stream.write('\n')
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temp, path)
    finally:
        if temp is not None:
            with contextlib.suppress(FileNotFoundError):
                temp.unlink()


class FileLock:
    """OS advisory lock: release survives process termination; file may remain."""
    def __init__(self, path):
        self.path, self.stream = Path(path), None

    def __enter__(self):
        self.path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        self.stream = open(self.path, 'a+b')
        try:
            if os.name == 'nt':
                import msvcrt
                self.stream.seek(0)
                self.stream.write(b'0')
                self.stream.flush()
                self.stream.seek(0)
                msvcrt.locking(self.stream.fileno(), msvcrt.LK_NBLCK, 1)
            else:
                import fcntl
                fcntl.flock(self.stream, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError as error:
            self.stream.close()
            raise AutomationError('TASK_LOCKED', 'Another process owns this task or upload index',
                                  details={'lock_file': str(self.path)}) from error
        return self

    def __exit__(self, *_):
        if os.name == 'nt':
            import msvcrt
            self.stream.seek(0)
            msvcrt.locking(self.stream.fileno(), msvcrt.LK_UNLCK, 1)
        else:
            import fcntl
            fcntl.flock(self.stream, fcntl.LOCK_UN)
        self.stream.close()


class TaskState:
    def __init__(self, path, data):
        self.path = Path(path).expanduser().resolve()
        self.data = data

    @classmethod
    def load(cls, path):
        path = Path(path).expanduser().resolve()
        try:
            data = json.loads(path.read_text(encoding='utf-8'))
        except (OSError, ValueError) as error:
            raise AutomationError('INVALID_STATE', f'Cannot read task state: {error}', exit_code=2) from error
        if not isinstance(data, dict) or data.get('schema_version') != 1 or data.get('platform') != 'youtube':
            raise AutomationError('INVALID_STATE', 'Unsupported or invalid YouTube state file', exit_code=2)
        for field in ['job_id', 'config', 'phase']:
            if field not in data:
                raise AutomationError('INVALID_STATE', f'State is missing {field}', exit_code=2)
        config = data['config']
        if not isinstance(config, dict) or not isinstance(data['job_id'], str) or not isinstance(data['phase'], str):
            raise AutomationError('INVALID_STATE', 'Invalid task/config types', exit_code=2)
        required = ['title','description','mode','visibility','state_dir','upload_timeout','processing_timeout','step_timeout','poll_interval']
        if any(k not in config for k in required) or config['mode'] not in {'draft','publish'} or config['visibility'] not in {'public','unlisted','private'}:
            raise AutomationError('INVALID_STATE', 'Missing or invalid task configuration', exit_code=2)
        for key in ['upload_timeout','processing_timeout','step_timeout','poll_interval']:
            value = config[key]
            if not isinstance(value, (int,float)) or not 0 < value < float('inf'):
                raise AutomationError('INVALID_STATE', f'Invalid {key}', exit_code=2)
        if any(not isinstance(config[k], str) for k in ['title','description','state_dir']) or config.get('audience') not in {None,'made-for-kids','not-made-for-kids'}:
            raise AutomationError('INVALID_STATE', 'Invalid metadata or audience', exit_code=2)
        video = data.get('video')
        if (not isinstance(video, dict) or not isinstance(video.get('path'), str) or
            not isinstance(video.get('size'), int) or video['size'] <= 0 or
            not isinstance(video.get('mtime_ns'), int) or
            not isinstance(video.get('sha256'), str) or not re.fullmatch('[0-9a-f]{64}', video['sha256'])):
            raise AutomationError('INVALID_STATE', 'Invalid source video fingerprint', exit_code=2)
        if data.get('video_id') and not re.fullmatch(r'[A-Za-z0-9_-]{11}', str(data['video_id'])):
            raise AutomationError('INVALID_STATE', 'Invalid video ID', exit_code=2)
        return cls(path, data)

    def save(self, **changes):
        self.data.update(changes)
        self.data['updated_at'] = time.time()
        atomic_json(self.path, self.data)

    def intent(self, action):
        self.save(last_action={'name': action, 'outcome': 'pending', 'at': time.time()})

    def confirmed(self, **changes):
        action = dict(self.data.get('last_action') or {})
        action['outcome'] = 'confirmed'
        self.save(last_action=action, **changes)


def claim_upload(state, registry_directory, *, allow_duplicate=False):
    """Claim before file selection; never infer that a failed upload was rolled back."""
    directory = Path(registry_directory)
    key = state.data['video']['sha256'] + ':' + state.data['channel_id']
    with FileLock(directory / 'upload-index.lock'):
        path = directory / 'upload-index.json'
        try:
            index = json.loads(path.read_text(encoding='utf-8')) if path.exists() else {}
        except (OSError, ValueError) as error:
            raise AutomationError('INVALID_UPLOAD_INDEX', f'Cannot read duplicate-upload index: {error}') from error
        if not isinstance(index, dict):
            raise AutomationError('INVALID_UPLOAD_INDEX', 'Upload index must be an object')
        previous = index.get(key)
        if previous and previous != str(state.path) and not allow_duplicate:
            raise AutomationError('DUPLICATE_UPLOAD', 'This video already has an upload task for this channel; inspect/resume that task',
                                  exit_code=7, details={'existing_state_file': previous})
        index[key] = str(state.path)
        atomic_json(path, index)
        state.save(upload_intent=True)
