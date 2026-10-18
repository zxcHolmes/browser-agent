"""Local media inputs shared by platform upload CLIs: text files, file
fingerprints (for de-duplication), direct-URL downloads, and still images
(a frame taken from a video, or an image converted to fit a site's limits).
Image work needs ffmpeg on PATH only when a conversion is actually required."""
import contextlib
import hashlib
import os
from pathlib import Path
import re
import shutil
import subprocess
import sys
from http.client import HTTPException
from urllib.error import URLError
from urllib.parse import urlparse, unquote
from urllib.request import Request, urlopen

try:
    from .automation import AutomationError
except ImportError:
    from automation import AutomationError


def log(message, quiet=False):
    if not quiet:
        print(message, file=sys.stderr, flush=True)


def read_text(value, filename, default=''):
    if filename:
        try:
            return Path(filename).expanduser().read_text(encoding='utf-8-sig')
        except (OSError, UnicodeError) as error:
            raise AutomationError('INVALID_TEXT_FILE', str(error), exit_code=2) from error
    return value if value is not None else default


def fingerprint(path, quiet=False):
    path = Path(path).expanduser().resolve()
    try:
        before = path.stat()
        if not path.is_file() or before.st_size == 0:
            raise ValueError('视频必须是非空普通文件')
        log(f'检查视频文件并计算 SHA-256（{before.st_size:,} 字节）…', quiet)
        sha = hashlib.sha256()
        with path.open('rb') as stream:
            for chunk in iter(lambda: stream.read(1024 * 1024), b''):
                sha.update(chunk)
        after = path.stat()
        if (before.st_size, before.st_mtime_ns) != (after.st_size, after.st_mtime_ns):
            raise ValueError('文件在读取期间发生变化，请等待生成完成后重试')
        return {'path': str(path), 'size': after.st_size, 'mtime_ns': after.st_mtime_ns, 'sha256': sha.hexdigest()}
    except (OSError, ValueError) as error:
        raise AutomationError('INVALID_VIDEO', str(error), exit_code=2) from error


def download_video(url, directory, *, max_mb=20480, timeout=120, quiet=False, reject_hosts=(), user_agent='Browser-Agent-CLI/1.0'):
    """Download a direct media-file URL into directory. reject_hosts: sites whose
    URLs are player/share pages, never files (host or any subdomain)."""
    parsed = urlparse(url)
    if parsed.scheme not in {'http', 'https'} or not parsed.netloc or parsed.username:
        raise AutomationError('INVALID_VIDEO_URL', '视频 URL 必须是 HTTP(S) 直链，不能包含用户名密码', exit_code=2)
    host = parsed.hostname or ''
    if any(host == h or host.endswith('.' + h) for h in reject_hosts):
        raise AutomationError('NOT_DIRECT_VIDEO_URL', f'请使用视频文件直链；{host} 的播放/分享页不是视频文件', exit_code=2)
    directory = Path(directory)
    directory.mkdir(parents=True, exist_ok=True, mode=0o700)
    filename = Path(unquote(parsed.path)).name or 'video.mp4'
    filename = re.sub(r'[^\w.\-]', '_', filename)[:150] or 'video.mp4'
    path = directory / filename
    partial = directory / (filename + '.part')
    log('下载指定视频直链到本地任务目录…', quiet)
    try:
        request = Request(url, headers={'User-Agent': user_agent})
        with urlopen(request, timeout=timeout) as response, partial.open('wb') as stream:
            content_type = response.headers.get('Content-Type', '').lower()
            if any(t in content_type for t in ['text/html', 'application/json', 'text/plain']):
                raise ValueError(f'URL 返回 {content_type}，不是视频文件')
            total = response.headers.get('Content-Length')
            if total and int(total) > max_mb * 1024 * 1024:
                raise ValueError('文件超过 --max-download-mb 限制')
            size = 0
            for chunk in iter(lambda: response.read(1024 * 1024), b''):
                size += len(chunk)
                if size > max_mb * 1024 * 1024:
                    raise ValueError('下载超过 --max-download-mb 限制')
                stream.write(chunk)
            if total and size != int(total):
                raise ValueError('下载内容长度不完整')
            if size == 0:
                raise ValueError('下载为空文件')
        os.chmod(partial, 0o600)
        os.replace(partial, path)
        return path
    except (OSError, URLError, HTTPException, ValueError) as error:
        with contextlib.suppress(OSError):
            partial.unlink()
        raise AutomationError('DOWNLOAD_FAILED', str(error), exit_code=2) from error


def ffmpeg(*args):
    if not shutil.which('ffmpeg'):
        raise AutomationError('FFMPEG_REQUIRED', '需要 ffmpeg（截帧或转换图片格式/大小）', exit_code=2)
    result = subprocess.run(['ffmpeg', '-v', 'error', '-y', *map(str, args)], capture_output=True, text=True)
    if result.returncode:
        raise AutomationError('FFMPEG_FAILED', (result.stderr or 'ffmpeg failed').strip()[:1000], exit_code=2)


def extract_frame(video, seconds, out_path):
    """One JPEG frame of video at seconds."""
    out_path = Path(out_path)
    out_path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    with contextlib.suppress(FileNotFoundError):
        out_path.unlink()  # never hand back an earlier run's frame
    try:
        ffmpeg('-ss', f'{float(seconds):.3f}', '-i', Path(video).expanduser(), '-frames:v', '1', '-q:v', '2', out_path)
    except AutomationError as error:
        if error.code != 'FFMPEG_FAILED':
            raise
    if not out_path.is_file() or out_path.stat().st_size == 0:
        raise AutomationError('FRAME_EXTRACT_FAILED', f'没有在 {seconds}s 处截到画面（超过视频长度？）', exit_code=2)
    return out_path


def prepare_image(path, directory, *, max_bytes, formats=('.jpg', '.jpeg', '.png')):
    """path itself when it already fits (format and size); otherwise a JPEG copy
    in directory, re-encoded at falling quality until it is under max_bytes."""
    path = Path(path).expanduser().resolve()
    if not path.is_file() or path.stat().st_size == 0:
        raise AutomationError('INVALID_IMAGE', f'图片必须是非空普通文件：{path}', exit_code=2)
    if path.suffix.lower() in formats and path.stat().st_size <= max_bytes:
        return path
    for quality in (2, 4, 6, 9, 13):
        out = Path(directory) / (path.stem + '.jpg')
        out.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        ffmpeg('-i', path, '-frames:v', '1', '-q:v', quality, out)
        if out.stat().st_size <= max_bytes:
            return out
    raise AutomationError('IMAGE_TOO_LARGE', f'图片压缩后仍超过 {max_bytes // 1024 // 1024}MB', exit_code=2)
