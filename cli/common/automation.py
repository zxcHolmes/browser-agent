"""Reusable task state, locking, and stage-aware failures for browser CLIs."""
import contextlib
import json
import os
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
    def load(cls, path, *, platform, validate=None):
        """Read a job written by this platform's CLI. Generic checks only; pass
        validate(data) to check the platform's own fields (raise AutomationError)."""
        path = Path(path).expanduser().resolve()
        try:
            data = json.loads(path.read_text(encoding='utf-8'))
        except (OSError, ValueError) as error:
            raise AutomationError('INVALID_STATE', f'Cannot read task state: {error}', exit_code=2) from error
        if not isinstance(data, dict) or data.get('schema_version') != 1 or data.get('platform') != platform:
            raise AutomationError('INVALID_STATE', f'Not a {platform} task state file (or an unsupported version)', exit_code=2)
        for field in ['job_id', 'config', 'phase']:
            if field not in data:
                raise AutomationError('INVALID_STATE', f'State is missing {field}', exit_code=2)
        if not isinstance(data['config'], dict) or not isinstance(data['job_id'], str) or not isinstance(data['phase'], str):
            raise AutomationError('INVALID_STATE', 'Invalid task/config types', exit_code=2)
        for key in ['step_timeout', 'poll_interval']:
            value = data['config'].get(key)
            if not isinstance(value, (int, float)) or not 0 < value < float('inf'):
                raise AutomationError('INVALID_STATE', f'Invalid {key}', exit_code=2)
        if validate:
            validate(data)
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


def claim_key(state, registry_directory, key, *, allow_duplicate=False, index_name='upload-index'):
    """Record that this job owns `key` (e.g. content hash + destination account)
    before the first irreversible step. Another job holding the same key means
    the same content was already sent there; never infer that it was rolled back."""
    directory = Path(registry_directory)
    with FileLock(directory / f'{index_name}.lock'):
        path = directory / f'{index_name}.json'
        try:
            index = json.loads(path.read_text(encoding='utf-8')) if path.exists() else {}
        except (OSError, ValueError) as error:
            raise AutomationError('INVALID_UPLOAD_INDEX', f'Cannot read duplicate index: {error}') from error
        if not isinstance(index, dict):
            raise AutomationError('INVALID_UPLOAD_INDEX', 'Duplicate index must be an object')
        previous = index.get(key)
        if previous and previous != str(state.path) and not allow_duplicate:
            raise AutomationError('DUPLICATE_UPLOAD', 'The same content already has a task for this destination; inspect or resume that task',
                                  exit_code=7, details={'existing_state_file': previous})
        index[key] = str(state.path)
        atomic_json(path, index)
