"""Private, bounded image copies for cooldown replay; never uploads by itself."""
from __future__ import annotations
import hashlib
import os
from pathlib import Path
import threading

MAX_IMAGE_BYTES=8*1024*1024
MAX_CACHE_BYTES=128*1024*1024


def image_suffix(data):
    if data.startswith(b'\x89PNG\r\n\x1a\n'): return '.png'
    if data.startswith(b'\xff\xd8\xff'): return '.jpg'
    if data.startswith((b'GIF87a',b'GIF89a')): return '.gif'
    if data[:4]==b'RIFF' and data[8:12]==b'WEBP': return '.webp'
    raise ValueError('Unsupported image content')


class VisionInbox:
    def __init__(self,path):
        self.path=Path(path).resolve()
        self.path.mkdir(parents=True,exist_ok=True,mode=0o700)
        self._lock=threading.RLock()

    def stash(self,source):
        with self._lock:
            return self._stash(source)

    def _stash(self,source):
        source=Path(source)
        if source.is_symlink() or not source.is_file():
            raise ValueError('Image must be a regular file')
        size=source.stat().st_size
        if not 0<size<=MAX_IMAGE_BYTES:
            raise ValueError('Image size is outside the private cache limit')
        data=source.read_bytes()
        if len(data)>MAX_IMAGE_BYTES: raise ValueError('Image grew beyond the cache limit')
        suffix=image_suffix(data)
        target=self.path/(hashlib.sha256(data).hexdigest()+suffix)
        if target.parent.resolve()!=self.path:
            raise ValueError('Image target outside the private inbox')
        if target.exists():
            if target.is_symlink() or target.read_bytes()!=data:
                raise ValueError('Cached image path changed unexpectedly')
            return str(target)
        total=sum(item.stat().st_size for item in self.path.iterdir() if item.is_file())
        if total+len(data)>MAX_CACHE_BYTES:
            raise ValueError('Private image cache is full; no existing data was deleted')
        pending=target.with_suffix(suffix+'.pending')
        with pending.open('xb') as handle:
            os.chmod(str(pending),0o600)
            handle.write(data)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(str(pending),str(target))
        return str(target)

    def validate(self,reference):
        path=Path(reference)
        if path.is_symlink() or path.parent.resolve()!=self.path or not path.is_file():
            raise ValueError('Image reference is not an owned private image')
        data=path.read_bytes()
        if not 0<len(data)<=MAX_IMAGE_BYTES or path.name!=hashlib.sha256(data).hexdigest()+image_suffix(data):
            raise ValueError('Owned image reference failed its content check')
        return str(path)
