"""What every open document shares: the store, the document engine and the render cache.

The cache key includes the engine's version (cache.py), and asking a stopped engine for it starts
the engine. The version is therefore remembered from the last start, next to the engine binary's
identity, so a document opened again right after launch comes from the cache while the engine is
still warming up instead of waiting 5-10 s for it.
"""

from __future__ import annotations

import importlib.util
import json
import logging
import os
import shutil
import tempfile
import threading
from pathlib import Path

from sovander import paths
from sovander.cache import RenderCache, content_key, font_set_hash
from sovander.engine import process
from sovander.engine.process import Engine
from sovander.store import local_path

log = logging.getLogger(__name__)

VERSION_FILE = "engine-version.json"
MB = 1000 * 1000


class Context:
    """Shared by the document pages of one app; safe to use from worker threads."""

    def __init__(self, store=None, engine: Engine | None = None, cache_root: Path | None = None):
        self.store = store
        self._engine = engine
        self._cache_root = cache_root
        self._lock = threading.Lock()
        self._caches: dict[tuple[str, bool], RenderCache] = {}
        self._fonts: str | None = None

    @property
    def engine(self) -> Engine:
        return self._engine if self._engine is not None else process.shared()

    @property
    def night(self) -> bool:
        return bool(self.store is not None and self.store.settings.night_mode)

    @property
    def text_page_bytes(self) -> int:
        return (self.store.settings.text_page_mb if self.store is not None else 5) * MB

    def cache(self, engine: Engine) -> RenderCache | None:
        """The render cache for ``engine``'s version, or None while that version is unknown.

        Call from a worker thread: the first call hashes the font folders.
        """
        if engine.alive:
            version = engine.version
            remember_version(engine, version)
        else:
            version = remembered_version(engine)
        if version is None:
            return None
        memory_only = self.store is not None and not self.store.settings.history_enabled  # 11.5
        with self._lock:
            if self._fonts is None:
                self._fonts = font_set_hash()
            key = (version, memory_only)
            if key not in self._caches:
                root = self._cache_root
                self._caches[key] = RenderCache(version, self._fonts, root=root, memory_only=memory_only)
                if self.store is not None and not memory_only:
                    from sovander import thumbs

                    thumbs.for_store(self.store).render_cache = self._caches[key]  # office thumbnails
            return self._caches[key]

    def forget(self, event) -> None:
        """Store's forget hook: removed or cleared recents take their renders along (11.3)."""
        threading.Thread(target=self._purge, args=(event,), name="sovander-purge", daemon=True).start()

    def _purge(self, event) -> None:
        with self._lock:
            caches = list(self._caches.values())
        # Any version and font set: purge and clear work on the whole folder, not one variant
        caches.append(RenderCache("any", "any", root=self._cache_root))
        for cache in caches:
            if event.everything:
                cache.clear()
                continue
            for uri in event.uris:
                path = local_path(uri)
                try:
                    if path is not None and path.is_file():
                        cache.purge(content_key(path))
                except OSError:
                    pass


def engine_missing() -> bool:
    """True when LibreOffice or its Python bridge is not installed, so Install is the advice."""
    return process.find_soffice() is None or importlib.util.find_spec("uno") is None


def _stamp(engine: Engine) -> list | None:
    """The engine binary's real path and modification time: an upgrade replaces the file."""
    if not engine.soffice:
        return None
    real = os.path.realpath(shutil.which(engine.soffice) or engine.soffice)
    try:
        return [real, os.stat(real).st_mtime_ns]
    except OSError:
        return None


_remembered: dict | None = None
_remember_lock = threading.Lock()


def remembered_version(engine: Engine) -> str | None:
    """The version the same engine binary reported when it last ran, or None."""
    global _remembered
    stamp = _stamp(engine)
    if stamp is None:
        return None
    with _remember_lock:
        if _remembered is None:
            try:
                _remembered = json.loads((paths.cache_dir() / VERSION_FILE).read_text(encoding="utf-8"))
            except (OSError, ValueError):
                _remembered = {}
        entry = _remembered if isinstance(_remembered, dict) else {}
    version = entry.get("version")
    return version if entry.get("stamp") == stamp and isinstance(version, str) else None


def remember_version(engine: Engine, version: str) -> None:
    global _remembered
    stamp = _stamp(engine)
    if stamp is None:
        return
    entry = {"stamp": stamp, "version": version}
    with _remember_lock:
        if _remembered == entry:
            return
        _remembered = entry
        folder = paths.cache_dir()
        try:
            fd, tmp = tempfile.mkstemp(dir=folder, prefix=".tmp-")
            with os.fdopen(fd, "w", encoding="utf-8") as fh:
                json.dump(entry, fh)
            os.replace(tmp, folder / VERSION_FILE)
        except OSError as exc:
            log.warning("could not remember the engine version: %s", exc)
