"""Drawing PDF pages off the main thread, for the page view and presentation mode (Requirements 2.1, 5).

MuPDF (PyMuPDF) draws pages to Gdk textures on one worker thread per view. PyMuPDF is not thread-safe,
so every fitz call, from any thread or module (search, selection, links), holds FITZ_LOCK.
"""

import logging
import math
import threading
from pathlib import Path

import gi

from sovander.i18n import _
from sovander.views import night

try:
    import pymupdf as fitz
except ImportError:  # PyMuPDF before 1.24 (Ubuntu 22.04 and 24.04) has only the old name
    import fitz

gi.require_version("Gdk", "4.0")
from gi.repository import Gdk, GLib  # noqa: E402

log = logging.getLogger(__name__)

FITZ_LOCK = threading.RLock()
TILE = 1024  # device pixels; large pages at high zoom are drawn piece by piece
TEXTURE_BUDGET = 160 * 1024 * 1024  # bytes of drawn pixels kept per view; the furthest tiles go first


class PasswordRequired(ValueError):
    """The PDF is encrypted and no password, or a wrong one, was given (Requirements 3.8, 3.9)."""


def open_pdf(pdf, password=None):
    """Open PDF bytes or a path read-only; raises PasswordRequired."""
    with FITZ_LOCK:
        if isinstance(pdf, (bytes, bytearray, memoryview)):
            doc = fitz.open(stream=bytes(pdf), filetype="pdf")
        else:
            doc = fitz.open(str(Path(pdf)), filetype="pdf")
        if doc.needs_pass and not doc.authenticate(password or ""):
            doc.close()
            raise PasswordRequired(_("This PDF needs a password"))
    return doc


def render_texture(doc, index, scale, clip=None, night_mode=False, alive=None):
    """Draw page ``index`` (or its ``clip``, in points) at ``scale`` device pixels per point.

    Returns (texture, drawn rect in points, byte size), or None when ``alive()`` says the job is stale.
    Safe from any thread: holds FITZ_LOCK for the fitz calls only, not for night mode.
    """
    with FITZ_LOCK:
        if doc.is_closed or (alive is not None and not alive()):
            return None
        page = doc[index]
        matrix = fitz.Matrix(scale, scale)
        pix = page.get_pixmap(matrix=matrix, clip=clip, alpha=False)
        boxes = night.image_boxes(page, matrix, (pix.x, pix.y)) if night_mode else ()
        data, x, y, w, h, stride = pix.samples, pix.x, pix.y, pix.width, pix.height, pix.stride
    if night_mode:
        data = night.invert_lightness(data, w, h, 3, boxes)
    texture = Gdk.MemoryTexture.new(w, h, Gdk.MemoryFormat.R8G8B8, GLib.Bytes.new(data), stride)
    return texture, (x / scale, y / scale, (x + w) / scale, (y + h) / scale), len(data)


def device_scale(widget) -> float:
    """Device pixels per logical pixel, fractional where GTK reports it (4.12+)."""
    native = widget.get_native()
    surface = native.get_surface() if native is not None else None
    if surface is not None and hasattr(surface, "get_scale"):
        return surface.get_scale()
    return widget.get_scale_factor()


class RenderWorker:
    """One background thread running the newest list of jobs, most wanted first.

    ``deliver(key, result)`` runs on the main thread; result is the job's return value or its exception.
    """

    def __init__(self, deliver):
        self._deliver, self._jobs, self._running, self._closed = deliver, [], set(), False
        self._cond = threading.Condition()
        threading.Thread(target=self._run, name="sovander-render", daemon=True).start()

    def submit(self, jobs):
        """Replace the waiting jobs with ``jobs``, a list of (key, callable) pairs."""
        with self._cond:
            self._jobs = list(jobs)
            self._cond.notify()

    def busy(self, key) -> bool:
        with self._cond:
            return key in self._running

    def close(self):
        with self._cond:
            self._closed, self._jobs = True, []
            self._cond.notify()

    def _run(self):
        while True:
            with self._cond:
                while not self._jobs and not self._closed:
                    self._cond.wait()
                if self._closed:
                    return
                key, job = self._jobs.pop(0)
                self._running.add(key)
            try:
                result = job()
            except Exception as exc:  # noqa: BLE001 - a damaged page must not stop the other pages
                result = exc
            GLib.idle_add(self._finish, key, result)

    def _finish(self, key, result):
        with self._cond:
            self._running.discard(key)
        if not self._closed:
            self._deliver(key, result)
        return GLib.SOURCE_REMOVE


class TileRenderer:
    """The drawn tiles of one document, keyed (page, gen, scale, tx, ty).

    ``gen`` changes with the document or night mode and ``scale`` with the zoom; tiles of an older
    gen or scale stay on screen as stand-ins until the page is covered by current ones.
    """

    def __init__(self, on_ready):
        self.doc, self.gen, self.scale, self.night, self.nbytes = None, 0, 0.0, False, 0
        self._on_ready, self._pages, self._failed = on_ready, {}, set()
        self._sizes, self._centre, self._keep = [], 0, range(0)
        self._worker = RenderWorker(self._deliver)

    def set_document(self, doc, keep_below=0):
        """Draw ``doc`` from now on; tiles of pages below ``keep_below`` stay until redrawn."""
        with FITZ_LOCK:
            old, self.doc, self.gen = self.doc, doc, self.gen + 1
            if old is not None and old is not doc:
                old.close()
        self._failed.clear()
        for page in [p for p in self._pages if p >= keep_below]:
            self._drop(page)

    def set_night(self, on):
        self.night, self.gen = on, self.gen + 1
        self._failed.clear()

    def keys(self, page, size, fx0, fy0, fx1, fy1):
        """Current-scale tiles covering the part (fx0..fx1, fy0..fy1, as fractions) of ``page``."""
        gw, gh = self._grid(size)
        x0, x1, y0, y1 = max(0.0, fx0) * gw, min(1.0, fx1) * gw, max(0.0, fy0) * gh, min(1.0, fy1) * gh
        if self.scale <= 0 or x1 <= x0 or y1 <= y0:
            return []
        return [
            (page, self.gen, self.scale, tx, ty)
            for ty in range(int(y0 // TILE), math.ceil(y1 / TILE))
            for tx in range(int(x0 // TILE), math.ceil(x1 / TILE))
        ]

    def request(self, wanted, sizes, keep, centre):
        """Draw the ``wanted`` keys in order; forget pages outside ``keep``; ``centre`` is on screen."""
        self._sizes, self._centre, self._keep = sizes, centre, keep
        for page in [p for p in self._pages if p not in keep]:
            self._drop(page)
        jobs = []
        for key in dict.fromkeys(wanted):
            if key not in self._failed and not self.has(key) and not self._worker.busy(key):
                jobs.append((key, self._job(key)))
        self._worker.submit(jobs)

    def has(self, key) -> bool:
        return key[1:] in self._pages.get(key[0], {})

    def has_current(self, page) -> bool:
        return any(k[:2] == (self.gen, self.scale) for k in self._pages.get(page, {}))

    def tiles(self, page):
        """(texture, rect in points) of ``page``, stand-ins first so the current tiles cover them."""
        current = (self.gen, self.scale)
        items = sorted(self._pages.get(page, {}).items(), key=lambda item: item[0][:2] == current)
        return [(texture, rect) for _key, (texture, rect, _n) in items]

    def close(self):
        self._worker.close()

    def _grid(self, size):
        return math.ceil(size[0] * self.scale), math.ceil(size[1] * self.scale)

    def _job(self, key):
        page, gen, scale, tx, ty = key
        gw, gh = self._grid(self._sizes[page])
        clip = fitz.Rect(tx * TILE, ty * TILE, min((tx + 1) * TILE, gw), min((ty + 1) * TILE, gh)) / scale
        doc, night_mode = self.doc, self.night
        return lambda: render_texture(doc, page, scale, clip, night_mode, alive=lambda: gen == self.gen)

    def _deliver(self, key, result):
        page, gen, scale, _tx, _ty = key
        if isinstance(result, Exception):
            log.warning("page %d could not be drawn: %s", page + 1, result)
            self._failed.add(key)
            return
        if result is None or (gen, scale) != (self.gen, self.scale) or page not in self._keep:
            return  # stale, or finished after the view moved away from it
        tiles = self._pages.setdefault(page, {})
        tiles[key[1:]] = result
        self.nbytes += result[2]
        gw, gh = self._grid(self._sizes[page])
        if sum(k[:2] == (gen, scale) for k in tiles) == math.ceil(gw / TILE) * math.ceil(gh / TILE):
            for stale in [k for k in tiles if k[:2] != (gen, scale)]:
                self._drop(page, stale)
        self._trim()
        self._on_ready(page)

    def _trim(self):
        current = (self.gen, self.scale)
        order = [
            (k[:2] == current, -abs(p - self._centre), p, k)
            for p, tiles in self._pages.items()
            for k in tiles
        ]
        for is_current, distance, page, key in sorted(order):
            if self.nbytes <= TEXTURE_BUDGET or (is_current and distance > -2):
                break  # never what is on screen
            self._drop(page, key)

    def _drop(self, page, key=None):
        tiles = self._pages.get(page, {})
        for k in [key] if key is not None else list(tiles):
            self.nbytes -= tiles.pop(k)[2]
        if not tiles:
            self._pages.pop(page, None)
