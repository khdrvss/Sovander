"""Render cache: the engine's PDFs kept by content, engine version and fonts (Requirements 9.10, 11.3, 11.5).

On disk: ``renders/<sha256 of the file>/<variant>/<part>``, where the variant hashes the engine version
and the installed font set, so either change is a miss. All renders of one document share a folder, so
``purge`` removes them at once. Least recently used documents go first once the cache passes its cap.
"""

from __future__ import annotations

import hashlib
import os
import re
import shutil
import tempfile
import threading
import time
from collections import OrderedDict
from pathlib import Path

from . import paths

CAP_BYTES = 1 << 30
PAGE1, FULL = "page1.pdf", "full.pdf"
FONT_DIRS = ("/usr/share/fonts", "/usr/local/share/fonts", "~/.local/share/fonts", "~/.fonts")
_KEY = re.compile(r"^[0-9a-f]{64}$")
_PART = re.compile(r"^[a-z0-9][a-z0-9._-]{0,63}$")
_TMP = ".tmp-"
_STALE_TMP_S = 3600


def content_key(source: Path | bytes) -> str:
    """sha256 of the file's bytes: the same document is the same key wherever it lives."""
    digest = hashlib.sha256()
    if isinstance(source, Path):
        with open(source, "rb") as fh:
            for block in iter(lambda: fh.read(1 << 20), b""):
                digest.update(block)
    else:
        digest.update(source)
    return digest.hexdigest()


def font_set_hash(dirs: tuple[str, ...] = FONT_DIRS) -> str:
    """Changes when fonts are added, removed or replaced (each changes its folder's mtime)."""
    digest = hashlib.sha256()
    for top in dirs:
        for dirpath, dirnames, _files in os.walk(os.path.expanduser(top)):
            dirnames.sort()
            try:
                digest.update(f"{dirpath}\0{os.stat(dirpath).st_mtime_ns}\n".encode())
            except OSError:
                continue
    return digest.hexdigest()[:16]


class RenderCache:
    """Page-1 and full PDFs per document. ``memory_only`` keeps nothing on disk (history off, 11.5)."""

    def __init__(self, engine_version: str, font_hash: str | None = None, root: Path | None = None,
                 cap_bytes: int = CAP_BYTES, memory_only: bool = False):
        fonts = font_set_hash() if font_hash is None else font_hash
        self.variant = hashlib.sha256(f"{engine_version}\0{fonts}".encode()).hexdigest()[:16]
        self.memory_only = memory_only
        self.root = None if memory_only else (root or paths.cache_dir() / "renders")
        self.cap_bytes = cap_bytes
        self._memory: OrderedDict[tuple[str, str], bytes] = OrderedDict()
        self._lock = threading.Lock()
        self._last_use = 0

    def get(self, key: str, part: str) -> bytes | None:
        """The stored bytes, or None; a hit makes the document the most recently used."""
        _check(key, part)
        with self._lock:
            if self.memory_only:
                data = self._memory.get((key, part))
                if data is not None:
                    self._memory.move_to_end((key, part))
                return data
            entry = self._entry(key)
            try:
                data = (entry / part).read_bytes()
            except OSError:
                return None
            self._touch(entry)
            return data

    def put(self, key: str, part: str, data: bytes) -> None:
        """Store atomically (a reader never sees half a file), then trim to the size cap."""
        _check(key, part)
        with self._lock:
            if self.memory_only:
                self._memory[(key, part)] = bytes(data)
                self._memory.move_to_end((key, part))
                self._trim_memory()
                return
            entry = self._entry(key)
            entry.mkdir(parents=True, exist_ok=True)
            fd, tmp = tempfile.mkstemp(dir=entry, prefix=_TMP)
            try:
                with os.fdopen(fd, "wb") as fh:
                    fh.write(data)
                os.replace(tmp, entry / part)
            except BaseException:
                Path(tmp).unlink(missing_ok=True)
                raise
            self._touch(entry)
            self._trim_disk(keep=entry)

    def purge(self, key: str) -> None:
        """Forget every render of one document, for any engine or font set (11.3)."""
        _check(key, PAGE1)
        with self._lock:
            for item in [k for k in self._memory if k[0] == key]:
                del self._memory[item]
            if self.root is not None:
                shutil.rmtree(self.root / key, ignore_errors=True)

    def clear(self) -> None:
        """Forget everything (clear history)."""
        with self._lock:
            self._memory.clear()
            if self.root is not None:
                shutil.rmtree(self.root, ignore_errors=True)

    def size(self) -> int:
        with self._lock:
            if self.memory_only:
                return sum(len(v) for v in self._memory.values())
            return sum(size for _path, size, _used in self._entries())

    def _entry(self, key: str) -> Path:
        return self.root / key / self.variant

    def _touch(self, entry: Path) -> None:
        """The folder's mtime is the document's last use; strictly increasing, unlike coarse file times."""
        self._last_use = max(time.time_ns(), self._last_use + 1)
        try:
            os.utime(entry, ns=(self._last_use, self._last_use))
        except OSError:
            pass

    def _entries(self) -> list[tuple[Path, int, int]]:
        """(variant folder, bytes, last use) for every stored render; stale temp files are removed."""
        found = []
        if self.root is None or not self.root.is_dir():
            return found
        now = time.time()
        for key_dir in self.root.iterdir():
            if not key_dir.is_dir():
                continue
            for entry in key_dir.iterdir():
                total = 0
                try:
                    for item in entry.iterdir():
                        stat = item.stat()
                        if item.name.startswith(_TMP) and now - stat.st_mtime > _STALE_TMP_S:
                            item.unlink(missing_ok=True)  # left by a crash mid-write
                            continue
                        total += stat.st_size
                    found.append((entry, total, entry.stat().st_mtime_ns))
                except OSError:
                    continue
        return found

    def _trim_disk(self, keep: Path) -> None:
        entries = self._entries()
        total = sum(size for _path, size, _used in entries)
        for entry, size, _used in sorted(entries, key=lambda e: e[2]):
            if total <= self.cap_bytes:
                break
            if entry == keep:
                continue  # the document being shown stays, even when it alone is over the cap
            shutil.rmtree(entry, ignore_errors=True)
            total -= size
            try:
                entry.parent.rmdir()  # the document folder, once its last variant is gone
            except OSError:
                pass

    def _trim_memory(self) -> None:
        total = sum(len(v) for v in self._memory.values())
        while total > self.cap_bytes and len(self._memory) > 1:
            _item, data = self._memory.popitem(last=False)
            total -= len(data)


def _check(key: str, part: str) -> None:
    if not _KEY.match(key) or not _PART.match(part):
        raise ValueError("cache keys are sha256 hex digests and parts are plain file names")
