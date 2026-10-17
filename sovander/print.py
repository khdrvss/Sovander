"""Printing what is shown (Requirement 12.5) through the system print dialog.

- Page view (PDFs and everything the engine lays out): Gtk.PrintOperation draws each page with
  MuPDF at the printer's resolution. Each sheet of paper takes the size and orientation of its PDF
  page, under a standard paper name where one matches; GTK passes only the pages the user picked.
- Web view (books, Markdown): WebKit's own printing, which paginates the page as laid out.
- Text view: the loaded text, wrapped and paginated in a monospace font.
- Image view: the image fitted to the printable area, turned to the paper's better orientation.

Anything else raises NotPrintable. Night mode is not printed: paper gets the document's own colours.
"""

from __future__ import annotations

import functools
import sys
from pathlib import Path

import gi

gi.require_version("Gdk", "4.0")
gi.require_version("GdkPixbuf", "2.0")
gi.require_version("Gtk", "4.0")
gi.require_version("Pango", "1.0")
gi.require_version("PangoCairo", "1.0")
from gi.repository import Gdk, GdkPixbuf, GLib, Gtk, Pango, PangoCairo  # noqa: E402

from sovander.i18n import _  # noqa: E402
from sovander.views.page_view import PageView  # noqa: E402

try:
    import pymupdf as fitz
except ImportError:  # PyMuPDF before 1.24 (Ubuntu 22.04 and 24.04) has only the old name
    import fitz

MIN_DPI, MAX_DPI = 150, 600  # export reports 72 dpi, which would blur; past 600 is memory, not detail
MAX_PIXELS = 60_000_000  # one page's raster: an A4 page at 600 dpi is about 35 million
TEXT_FONT = "Monospace 10"
PAGINATE_STEP = 2000  # paragraphs per paginate call, so the dialog stays responsive on 5 MB of text
_running: set = set()  # operations alive until they finish; GTK holds only a weak reference


class NotPrintable(TypeError):
    """This kind of view has no printing; ``str()`` is the sentence for the user."""

    def __init__(self):
        super().__init__(_("This document cannot be printed"))


def print_view(parent: Gtk.Window | None, view, *, settings: Gtk.PrintSettings | None = None,
               dialog: bool = True, export_to: str | Path | None = None):
    """Print ``view``: the system dialog by default, silently with ``dialog=False``, or into a PDF file.

    Returns the Gtk.PrintOperation or WebKit.PrintOperation; raises NotPrintable.
    """
    web = _module_class("sovander.views.web_view", "WebView")
    if web is not None and isinstance(view, web):
        return _print_web(parent, view, settings, dialog, export_to)
    if isinstance(view, PageView):
        job = _PdfJob(view)
    elif (text := _module_class("sovander.views.text_view", "TextView")) and isinstance(view, text):
        job = _TextJob(view.text)
    elif (image := _module_class("sovander.views.image_view", "ImageView")) and isinstance(view, image):
        if view.decoded is None:
            raise NotPrintable()
        job = _ImageJob(view.decoded.pixbuf)
    else:
        raise NotPrintable()
    return _run(job, parent, settings, dialog, export_to)


def _module_class(module: str, name: str):
    """A view class, if its module is loaded; a view of it can exist only if it is."""
    loaded = sys.modules.get(module)
    return getattr(loaded, name, None) if loaded else None


def _run(job, parent, settings, dialog, export_to) -> Gtk.PrintOperation:
    op = Gtk.PrintOperation(unit=Gtk.Unit.POINTS, embed_page_setup=True, show_progress=dialog)
    if settings is not None:
        op.set_print_settings(settings)
    job.attach(op)
    _running.add(op)
    op.connect("done", _forget)
    if export_to is not None:
        op.set_export_filename(str(export_to))
        op.run(Gtk.PrintOperationAction.EXPORT, parent)
        _running.discard(op)
    else:
        op.set_allow_async(True)
        action = Gtk.PrintOperationAction.PRINT_DIALOG if dialog else Gtk.PrintOperationAction.PRINT
        if op.run(action, parent) != Gtk.PrintOperationResult.IN_PROGRESS:
            _running.discard(op)
    return op


def _forget(op, _result) -> None:
    _running.discard(op)


# Page view


def _paper(width: float, height: float) -> Gtk.PaperSize:
    """A standard paper of this size in points (either way round), else a custom one."""
    return _paper_for(*sorted((round(width, 1), round(height, 1))))


@functools.lru_cache(maxsize=64)
def _paper_for(short: float, long: float) -> Gtk.PaperSize:
    for paper in Gtk.PaperSize.get_paper_sizes(False):
        w, h = paper.get_width(Gtk.Unit.POINTS), paper.get_height(Gtk.Unit.POINTS)
        if abs(min(w, h) - short) < 1.5 and abs(max(w, h) - long) < 1.5:
            return paper
    name = f"sovander_{round(short)}x{round(long)}pt"
    return Gtk.PaperSize.new_custom(name, name, short, long, Gtk.Unit.POINTS)


class _PdfJob:
    """Pages of a PageView's document, each on paper of its own size."""

    def __init__(self, view: PageView):
        self._view = view
        with view.lock:
            doc = view.document
            if doc is None or doc.is_closed or view.page_count < 1:
                raise NotPrintable()
            self._title = (doc.metadata or {}).get("title") or ""
        self._sizes = [view.page_size(i) for i in range(view.page_count)]  # read once by the view

    def attach(self, op: Gtk.PrintOperation) -> None:
        op.set_n_pages(len(self._sizes))
        op.set_use_full_page(True)
        op.set_current_page(min(self._view.current_page, len(self._sizes) - 1))
        op.set_job_name(self._title or "Sovander")
        setup = Gtk.PageSetup()
        self._shape(setup, 0)
        op.set_default_page_setup(setup)
        op.connect("request-page-setup", self._on_page_setup)
        op.connect("draw-page", self._on_draw)

    def _shape(self, setup: Gtk.PageSetup, page_nr: int) -> None:
        width, height = self._sizes[page_nr]
        setup.set_paper_size(_paper(width, height))
        landscape = width > height
        setup.set_orientation(Gtk.PageOrientation.LANDSCAPE if landscape else Gtk.PageOrientation.PORTRAIT)

    def _on_page_setup(self, _op, _context, page_nr, setup) -> None:
        if 0 <= page_nr < len(self._sizes):
            self._shape(setup, page_nr)

    def _on_draw(self, _op, context, page_nr) -> None:
        drawn = render_page(self._view, page_nr, context.get_dpi_x())
        if drawn is not None:
            _paint(context, *drawn)


def render_page(view: PageView, page_nr: int, dpi: float):
    """Page ``page_nr`` of the view's document as (pixbuf, width pt, height pt), or None if it is gone."""
    dpi = min(max(dpi, MIN_DPI), MAX_DPI)
    with view.lock:
        doc = view.document  # read again: a fuller copy may have been swapped in since
        if doc is None or doc.is_closed or not 0 <= page_nr < doc.page_count:
            return None
        page = doc[page_nr]
        width, height = page.rect.width, page.rect.height
        scale = min(dpi / 72, (MAX_PIXELS / max(width * height, 1)) ** 0.5)
        pix = page.get_pixmap(matrix=fitz.Matrix(scale, scale), alpha=False)
        data, w, h, stride = pix.samples, pix.width, pix.height, pix.stride
    rgb = GdkPixbuf.Colorspace.RGB
    return GdkPixbuf.Pixbuf.new_from_bytes(GLib.Bytes.new(data), rgb, False, 8, w, h, stride), width, height


def _paint(context: Gtk.PrintContext, pixbuf: GdkPixbuf.Pixbuf, width: float, height: float) -> None:
    """Draw ``pixbuf`` as a ``width`` x ``height`` point picture, fitted and centred on the page."""
    cr = context.get_cairo_context()
    room_w, room_h = context.get_width(), context.get_height()
    fit = min(room_w / width, room_h / height)
    cr.translate((room_w - width * fit) / 2, (room_h - height * fit) / 2)
    cr.scale(width * fit / pixbuf.get_width(), height * fit / pixbuf.get_height())
    Gdk.cairo_set_source_pixbuf(cr, pixbuf, 0, 0)
    cr.paint()


# Image view


class _ImageJob:
    def __init__(self, pixbuf: GdkPixbuf.Pixbuf):
        self._pixbuf = pixbuf

    def attach(self, op: Gtk.PrintOperation) -> None:
        op.set_n_pages(1)
        landscape = self._pixbuf.get_width() > self._pixbuf.get_height()
        setup = Gtk.PageSetup()
        setup.set_orientation(Gtk.PageOrientation.LANDSCAPE if landscape else Gtk.PageOrientation.PORTRAIT)
        op.set_default_page_setup(setup)
        op.connect("draw-page", self._on_draw)

    def _on_draw(self, _op, context, _page_nr) -> None:
        _paint(context, self._pixbuf, self._pixbuf.get_width(), self._pixbuf.get_height())


# Text view


class _TextJob:
    """Text wrapped to the printable width; pages are lists of (paragraph, line) pairs."""

    def __init__(self, text: str):
        self._paragraphs = text.split("\n")
        if self._paragraphs and self._paragraphs[-1] == "":
            self._paragraphs.pop()  # a final newline makes no empty last line
        self._layout: Pango.Layout | None = None
        self._laid = -1
        self.pages: list[list[tuple[int, int]]] = []
        self._page: list[tuple[int, int]] = []
        self._used = 0.0
        self._next = 0

    def attach(self, op: Gtk.PrintOperation) -> None:
        op.set_job_name("Sovander")
        op.connect("begin-print", self._on_begin)
        op.connect("paginate", self._on_paginate)
        op.connect("draw-page", self._on_draw)

    def _on_begin(self, _op, context) -> None:
        self._layout = context.create_pango_layout()
        self._layout.set_font_description(Pango.FontDescription.from_string(TEXT_FONT))
        self._layout.set_width(int(context.get_width() * Pango.SCALE))
        self._layout.set_wrap(Pango.WrapMode.WORD_CHAR)
        self._height = context.get_height()
        self.pages, self._page, self._used, self._next, self._laid = [], [], 0.0, 0, -1

    def _on_paginate(self, op, _context) -> bool:
        end = min(len(self._paragraphs), self._next + PAGINATE_STEP)
        for index in range(self._next, end):
            layout = self._lay(index)
            for line in range(layout.get_line_count()):
                height = _line_height(layout, line)
                if self._page and self._used + height > self._height:
                    self.pages.append(self._page)
                    self._page, self._used = [], 0.0
                self._page.append((index, line))
                self._used += height
        self._next = end
        if end < len(self._paragraphs):
            return False
        if self._page or not self.pages:
            self.pages.append(self._page)
        op.set_n_pages(len(self.pages))
        return True

    def _on_draw(self, _op, context, page_nr) -> None:
        if not 0 <= page_nr < len(self.pages):
            return
        cr = context.get_cairo_context()
        cr.set_source_rgb(0, 0, 0)
        y = 0.0
        for index, line in self.pages[page_nr]:
            layout = self._lay(index)
            _ink, logical = layout.get_line_readonly(line).get_extents()
            cr.move_to(0, y - logical.y / Pango.SCALE)  # logical.y is the ascent, above the baseline
            PangoCairo.show_layout_line(cr, layout.get_line_readonly(line))
            y += logical.height / Pango.SCALE

    def _lay(self, index: int) -> Pango.Layout:
        if index != self._laid:
            self._layout.set_text(self._paragraphs[index] or " ", -1)
            self._laid = index
        return self._layout


def _line_height(layout: Pango.Layout, line: int) -> float:
    _ink, logical = layout.get_line_readonly(line).get_extents()
    return logical.height / Pango.SCALE


# Web view


def _print_web(parent, view, settings, dialog, export_to):
    gi.require_version("WebKit", "6.0")
    from gi.repository import WebKit

    op = WebKit.PrintOperation.new(view.web)
    if export_to is not None:
        settings = Gtk.PrintSettings() if settings is None else settings.copy()
        # GTK's file printer, under the name GTK gives it in the current language
        settings.set_printer(GLib.dgettext("gtk40", "Print to File"))
        settings.set(Gtk.PRINT_SETTINGS_OUTPUT_URI, Path(export_to).resolve().as_uri())
        settings.set(Gtk.PRINT_SETTINGS_OUTPUT_FILE_FORMAT, "pdf")
        dialog = False
    if settings is not None:
        op.set_print_settings(settings)
    _running.add(op)
    op.connect("finished", _forget_web)
    op.connect("failed", _forget_web)
    if dialog:
        # Runs the dialog in a nested loop and returns when it closes; printing then goes on alone
        if op.run_dialog(parent) == WebKit.PrintOperationResponse.CANCEL:
            _running.discard(op)
    else:
        op.print_()
    return op


def _forget_web(op, *_args) -> None:
    _running.discard(op)
