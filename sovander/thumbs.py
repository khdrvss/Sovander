"""Thumbnails of recent files for the home screen (Requirements 9.10, 11.1, 11.3, 11.5).

PNG files in ``paths.cache_dir()/"thumbs"``, named by the key Store keeps with each recent entry
(``Recent.thumb_key``). They are drawn on a worker thread: page 1 of a PDF, page 1 of the engine's
cached render of an office file, or the image itself, scaled down. Any other kind shows a generic
icon (``icon_name``). Nothing is drawn while history is off, and Store's forget hook deletes the
thumbnail of every entry that is removed, cleared or pushed off the list.

The app shell gives one ``Thumbnails`` per Store through ``for_store``. Once the engine's render
cache exists it sets ``render_cache`` on it, and after recording an open it may call ``remember``
with the page-1 PDF it already has, so office files get their thumbnail without a second render.
"""

from __future__ import annotations

import hashlib
import logging
import os
import queue
import re
import tempfile
import threading
import weakref
from collections.abc import Callable
from pathlib import Path

import gi

gi.require_version("GdkPixbuf", "2.0")
from gi.repository import GdkPixbuf, GLib  # noqa: E402

from sovander import paths  # noqa: E402
from sovander.cache import FULL, PAGE1, content_key  # noqa: E402
from sovander.store import Forgotten, Recent, Store, doc_key, local_path  # noqa: E402
from sovander.views import to_main_loop  # noqa: E402
from sovander.views.render import FITZ_LOCK  # noqa: E402

try:
    import pymupdf as fitz
except ImportError:  # PyMuPDF before 1.24 (Ubuntu 22.04 and 24.04) has only the old name
    import fitz

log = logging.getLogger(__name__)

THUMB_PX = 256  # longest side; cards show them at half size, sharp on a 2x screen
MAX_IMAGE_BYTES = 64 * 1024 * 1024
MAX_IMAGE_PIXELS = 150_000_000  # as the image view: bigger images are refused before decoding
IDLE_EXIT_S = 2.0

PDF_KINDS = frozenset({"pdf"})
IMAGE_KINDS = frozenset({"image"})
ENGINE_KINDS = frozenset({"word", "word-legacy", "odt", "rtf", "slides", "drawing"})

_ICONS = {
    "pdf": "x-office-document-symbolic",
    "word": "x-office-document-symbolic",
    "word-legacy": "x-office-document-symbolic",
    "odt": "x-office-document-symbolic",
    "rtf": "x-office-document-symbolic",
    "epub": "x-office-document-symbolic",
    "sheet": "x-office-spreadsheet-symbolic",
    "slides": "x-office-presentation-symbolic",
    "drawing": "x-office-drawing-symbolic",
    "image": "image-x-generic-symbolic",
    "markdown": "text-x-generic-symbolic",
    "text": "text-x-generic-symbolic",
    "zip": "package-x-generic-symbolic",
}
_GENERIC_ICON = "text-x-generic-symbolic"
_KEY = re.compile(r"^[0-9a-f]{32}$")

Done = Callable[[str, "Path | None"], object]


def icon_name(kind: str) -> str:
    """The symbolic icon for a kind (router.Kind values) that has no thumbnail."""
    return _ICONS.get(kind, _GENERIC_ICON)


def thumb_key(uri: str, key: str | None) -> str:
    """The file name of an entry's thumbnail: a new one when the file's content changes."""
    return hashlib.sha256(f"{uri}\0{key or ''}".encode()).hexdigest()[:32]


def pdf_png(pdf: Path | bytes, size: int = THUMB_PX) -> bytes | None:
    """Page 1 of a PDF as PNG, longest side ``size``; None when it is locked, empty or damaged."""
    try:
        with FITZ_LOCK:
            if isinstance(pdf, bytes):
                doc = fitz.open(stream=bytes(pdf), filetype="pdf")  # PyMuPDF < 1.24 takes no bytes subclass
            else:
                doc = fitz.open(str(pdf), filetype="pdf")
            try:
                if doc.needs_pass or doc.page_count < 1:
                    return None
                page = doc[0]
                scale = size / max(page.rect.width, page.rect.height, 1)
                return page.get_pixmap(matrix=fitz.Matrix(scale, scale), alpha=False).tobytes("png")
            finally:
                doc.close()
    except Exception as e:  # noqa: BLE001 - MuPDF raises plain RuntimeError for any damage
        log.info("no PDF thumbnail: %s", e)
        return None


def image_png(data: bytes, size: int = THUMB_PX) -> bytes | None:
    """An image scaled to fit ``size`` as PNG, decoded from bytes (an SVG gets no base folder)."""
    if not data:
        return None
    loader = GdkPixbuf.PixbufLoader()
    refused = []

    def on_size(_loader, width, height):
        if width * height > MAX_IMAGE_PIXELS:
            refused.append(True)
            loader.set_size(0, 0)  # gdk-pixbuf's "allocate nothing"
        elif max(width, height) > size:
            shrink = size / max(width, height)
            loader.set_size(max(1, round(width * shrink)), max(1, round(height * shrink)))

    loader.connect("size-prepared", on_size)
    try:
        loader.write(data)
        loader.close()
        pixbuf = loader.get_pixbuf()
        if refused or pixbuf is None:
            return None
        pixbuf = pixbuf.apply_embedded_orientation() or pixbuf
        ok, png = pixbuf.save_to_bufferv("png", [], [])
        return bytes(png) if ok else None
    except GLib.Error as e:
        log.info("no image thumbnail: %s", e.message)
        try:
            loader.close()
        except GLib.Error:
            pass
        return None


class Thumbnails:
    """Thumbnails of one Store's recent files; methods are safe to call from the main thread."""

    def __init__(self, store: Store, folder: Path | None = None, render_cache=None):
        # Weak: Store holds this object through its forget hook, and for_store keys on the Store
        self._store = weakref.ref(store)
        self._folder = Path(folder) if folder else paths.cache_dir() / "thumbs"
        self.render_cache = render_cache
        self._jobs: queue.SimpleQueue = queue.SimpleQueue()
        self._lock = threading.Condition()
        self._pending = 0
        self._worker: threading.Thread | None = None
        self._tried: set[str] = set()
        self._forgets = 0
        store.add_forget_hook(self._on_forget)

    @property
    def folder(self) -> Path:
        return self._folder

    def path(self, recent: Recent) -> Path | None:
        """The entry's PNG on disk, or None."""
        if not recent.thumb_key or not _KEY.match(recent.thumb_key):
            return None
        png = self._folder / f"{recent.thumb_key}.png"
        return png if png.is_file() else None

    def can_draw(self, kind: str) -> bool:
        if kind in ENGINE_KINDS:
            return self.render_cache is not None
        return kind in PDF_KINDS or kind in IMAGE_KINDS

    def request(self, recent: Recent, done: Done | None = None) -> bool:
        """Draw a missing thumbnail in the background; ``done(uri, png or None)`` runs on the main loop.

        Returns False when there is nothing to draw: history off, file missing or not local, a kind
        with only an icon, or a file already tried this session.
        """
        store = self._store()
        path = local_path(recent.uri)
        if store is None or not store.settings.history_enabled or recent.missing or path is None:
            return False
        if not self.can_draw(recent.kind) or recent.uri in self._tried:
            return False
        self._tried.add(recent.uri)
        self._submit((recent.uri, recent.kind, path, None, recent.doc_key, done))
        return True

    def remember(self, uri: str, kind: str, *, path: Path | None = None, pdf: bytes | None = None,
                 done: Done | None = None) -> None:
        """Draw an entry's thumbnail now, from its file or from page-1 PDF bytes the engine made.

        Call it after ``Store.record_open``: the key is stored with the entry, so an entry that
        is not listed gets no thumbnail.
        """
        store = self._store()
        if store is None or not store.settings.history_enabled:
            return
        self._tried.discard(uri)
        self._submit((uri, kind, path or local_path(uri), pdf, None, done))

    def prune(self) -> None:
        """Delete, in the background, thumbnails no recent entry names (a crash or a changed file)."""
        self._submit(None)

    def join(self, timeout: float = 10.0) -> bool:
        """Wait until every queued job is done; False on timeout."""
        with self._lock:
            return self._lock.wait_for(lambda: self._pending == 0, timeout)

    # Worker

    def _submit(self, job) -> None:
        with self._lock:
            self._pending += 1
            self._jobs.put((self._forgets, job))
            if self._worker is None:
                self._worker = threading.Thread(target=self._run, name="sovander-thumbs", daemon=True)
                self._worker.start()

    def _run(self) -> None:
        while True:
            try:
                forgets, job = self._jobs.get(timeout=IDLE_EXIT_S)
            except queue.Empty:
                with self._lock:
                    if self._jobs.empty():
                        self._worker = None
                        return
                continue
            try:
                if job is None:
                    self._prune()
                else:
                    self._draw(forgets, *job)
            except Exception:  # noqa: BLE001 - one bad file must not stop the thumbnails after it
                log.exception("thumbnail job failed")
            finally:
                with self._lock:
                    self._pending -= 1
                    self._lock.notify_all()

    def _draw(self, forgets, uri, kind, path, pdf, known_key, done) -> None:
        png_path = None
        store = self._store()
        if store is not None and store.settings.history_enabled:
            png = self._render(kind, path, pdf)
            if png is not None:
                key = known_key or (doc_key(path) if path else None) or (doc_key(pdf) if pdf else None)
                png_path = self._write(thumb_key(uri, key), png)
                store.set_thumb_key(uri, png_path.stem)
                if forgets != self._forgets or not store.settings.history_enabled:
                    # Forgotten or switched off while drawing: keep nothing behind (11.3, 11.5)
                    if not any(r.uri == uri for r in store.recents()) or not store.settings.history_enabled:
                        png_path.unlink(missing_ok=True)
                        png_path = None
        if done is not None:
            to_main_loop(_deliver, done, uri, png_path)

    def _render(self, kind: str, path: Path | None, pdf: bytes | None) -> bytes | None:
        if pdf is not None:
            return pdf_png(pdf)
        if path is None:
            return None
        if kind in PDF_KINDS:
            return pdf_png(path)
        if kind in IMAGE_KINDS:
            try:
                if path.stat().st_size > MAX_IMAGE_BYTES:
                    return None
                return image_png(path.read_bytes())
            except OSError:
                return None
        if kind in ENGINE_KINDS and self.render_cache is not None:
            try:
                key = content_key(path)
            except OSError:
                return None
            data = self.render_cache.get(key, PAGE1) or self.render_cache.get(key, FULL)
            return pdf_png(data) if data else None
        return None

    def _write(self, key: str, png: bytes) -> Path:
        self._folder.mkdir(parents=True, exist_ok=True)
        target = self._folder / f"{key}.png"
        fd, tmp = tempfile.mkstemp(dir=self._folder, prefix=".thumb-", suffix=".tmp")
        try:
            with os.fdopen(fd, "wb") as fh:
                fh.write(png)
            os.replace(tmp, target)
        except BaseException:
            Path(tmp).unlink(missing_ok=True)
            raise
        return target

    def _prune(self) -> None:
        store = self._store()
        if store is None or not self._folder.is_dir():
            return
        keep = {r.thumb_key for r in store.recents() if r.thumb_key}
        for png in self._folder.glob("*.png"):
            if png.stem not in keep:
                png.unlink(missing_ok=True)

    # Store hook (11.3); may run on any thread

    def _on_forget(self, event: Forgotten) -> None:
        with self._lock:
            self._forgets += 1
        self._tried.difference_update(event.uris)
        if event.everything:
            if self._folder.is_dir():
                for png in self._folder.glob("*.png"):
                    png.unlink(missing_ok=True)
            return
        for key in event.thumb_keys:
            if _KEY.match(key):
                (self._folder / f"{key}.png").unlink(missing_ok=True)


def _deliver(done: Done, uri: str, png: Path | None) -> bool:
    done(uri, png)
    return GLib.SOURCE_REMOVE


_shared: weakref.WeakKeyDictionary = weakref.WeakKeyDictionary()
_shared_lock = threading.Lock()


def for_store(store: Store) -> Thumbnails:
    """The one Thumbnails of ``store``, made on first use (its forget hook is added once)."""
    with _shared_lock:
        thumbs = _shared.get(store)
        if thumbs is None:
            thumbs = _shared[store] = Thumbnails(store)
        return thumbs
