"""The open pipeline: a file or a zip entry, routed by content to the view that shows it.

``DocumentPage`` is one open document (Requirements 1.6, 2, 3.8-3.10, 5.7, 7, 8.2, 10.2, 11.2). Until its
view is ready it shows a loading, password, error or "not supported" page; notes about macros or
damage sit above the view. Reading files and all engine work run on worker threads. Their results
come back through GLib idle callbacks that hold the page weakly and carry a token, so a closed
window is freed mid-conversion and an answer to an earlier attempt is dropped.
"""

from __future__ import annotations

import functools
import logging
import os
import stat
import threading
import time
import weakref
from collections.abc import Callable

import gi

gi.require_version("Gtk", "4.0")
from gi.repository import GLib, GObject, Gtk  # noqa: E402

from sovander import i18n, router, sheets_fast, status_pages  # noqa: E402
from sovander.document_context import Context, engine_missing  # noqa: E402
from sovander.engine import client  # noqa: E402
from sovander.engine.process import EngineUnavailable  # noqa: E402
from sovander.i18n import N_, _  # noqa: E402
from sovander.open_jobs import (  # noqa: E402
    ENGINE_KINDS,
    NotARegularFile,
    Source,
    deliver,
    office_job,
    read_job,
    reason_for,
    run_job,
)
from sovander.router import Kind  # noqa: E402
from sovander.store import doc_key  # noqa: E402
from sovander.views import weak_handler  # noqa: E402
from sovander.views.image_view import ImageView  # noqa: E402
from sovander.views.page_view import FITZ_LOCK, PageView, PasswordRequired  # noqa: E402
from sovander.views.sheet_view import SheetView  # noqa: E402
from sovander.views.text_view import SHOWN_AS_TEXT, TextView, detect_encoding  # noqa: E402

__all__ = ["Context", "DocumentPage", "Source"]

log = logging.getLogger(__name__)

DAMAGED_NOTE = N_("This file is damaged. Showing the readable part.")


class DocumentPage(Gtk.Box):
    """One open document: the pipeline's progress on screen, then the view that shows it."""

    __gtype_name__ = "SovanderDocumentPage"
    __gsignals__ = {"view-changed": (GObject.SignalFlags.RUN_FIRST, None, ())}

    def __init__(self, source: Source, context: Context | None = None,
                 on_open_entry: Callable[[str, bytes], object] | None = None):
        super().__init__(orientation=Gtk.Orientation.VERTICAL, hexpand=True, vexpand=True)
        self.source, self.context, self.on_open_entry = source, context or Context(), on_open_entry
        self.kind: Kind | None = None
        self.view: Gtk.Widget | None = None
        self.state = "new"  # loading, password, error, unsupported, ready
        self.error_text: str | None = None
        self.wrong_password = False
        self.password_entry: Gtk.PasswordEntry | None = None
        self._password: str | None = None
        self._token = 0
        self._doc_key: str | None = None
        self._thumb_uri: str | None = None  # a recent entry still without its thumbnail
        self._saved = None  # store.Position to go back to (11.2)
        self._restored = False
        self._layer = self._backend = None
        self._notes: dict[str, tuple[Callable[[], str], Gtk.Label]] = {}
        self._rebuild: Callable[[], Gtk.Widget] | None = None
        self._began, self._reported = time.monotonic(), False
        self._notes_box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL)
        self._slot = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, hexpand=True, vexpand=True)
        self.append(self._notes_box)
        self.append(self._slot)
        i18n.add_listener(self._relabel)

    # Starting

    def start(self) -> None:
        """Route the source and start showing it; failures become a message on this page (1.6)."""
        try:
            self.kind = self._route()
        except OSError as exc:
            self._fail(exc)
            return
        self._remember()
        self._open()

    def _route(self) -> Kind:
        src = self.source
        if src.data is not None:
            return router.detect_bytes(src.data, src.name)
        if src.path is None:
            raise FileNotFoundError(src.name)
        if not stat.S_ISREG(os.stat(src.path).st_mode):
            raise NotARegularFile(src.name)
        return router.detect_path(src.path)

    def _open(self) -> None:
        self._token += 1  # whatever an earlier attempt still sends is dropped
        kind = self.kind
        if kind is Kind.PDF:
            self._open_pdf()
        elif kind in ENGINE_KINDS:
            self._show_loading()
            self._spawn(office_job, self.source, kind, self._password, self.context)
        elif kind is Kind.SHEET:
            view = SheetView()
            view.set_loading(True)
            self._set_view(view, view, ready=False)
            self._spawn(sheets_fast.sheet_job, self.source, self._password, self.context)
        elif kind is Kind.TEXT:
            self._show_text(note=not router.is_known_text_name(self.source.name))
        elif kind is Kind.UNKNOWN_BINARY:
            self._show_unsupported()
        else:  # images, books, Markdown and archives are read off the main thread
            self._show_loading()
            self._spawn(read_job, self.source, kind)

    def _spawn(self, job, *args) -> None:
        token, ref = self._token, weakref.ref(self)

        def post(method: str, *values) -> None:
            GLib.idle_add(deliver, ref, token, method, values)

        def alive() -> bool:
            page = ref()
            return page is not None and page._token == token

        threading.Thread(target=run_job, args=(job, post, alive, args), name="sovander-open",
                         daemon=True).start()

    # PDF and engine pages (2.1-2.5, 2.10, 3.8-3.10, 5.7)

    def _open_pdf(self) -> None:
        view, widget = self._new_page_view()
        try:
            view.load(self.source.path if self.source.data is None else self.source.data, self._password)
            with FITZ_LOCK:
                repaired = bool(getattr(view.document, "is_repaired", False))
        except (PasswordRequired, OSError) as exc:
            self._fail(exc)
            return
        except Exception as exc:  # noqa: BLE001 - MuPDF's errors for a broken file differ by version
            log.info("%s: PDF not readable: %s", self.source.name, exc)
            self._fail(client.DamagedFile(str(exc)))
            return
        self._set_view(view, widget)
        if repaired:
            self._add_note("damaged", DAMAGED_NOTE)
        self._restore()

    def _new_page_view(self) -> tuple[PageView, Gtk.Widget]:
        from sovander.links import Links
        from sovander.views.selection import HighlightLayer, Selection

        view = PageView()
        if self.kind is Kind.SLIDES:
            view.mode = "slides"
            view.fit_page()  # a slide is wider than most windows at its printed size; show it whole
        view.night = self.context.night
        scrolled = Gtk.ScrolledWindow(child=view, hexpand=True, vexpand=True)
        self._layer = HighlightLayer(view)
        Selection(view, self._layer)
        Links(view)  # web links ask first (9.3-9.5), internal ones scroll (3.11)
        return view, self._layer.wrap(scrolled)

    def _show_pdf(self, pdf: bytes, final: bool, damaged: bool) -> None:
        """Page 1 arrives first with a loading row; the whole document then replaces it (5.7)."""
        view = self.view if isinstance(self.view, PageView) else None
        if view is None:
            view, widget = self._new_page_view()
            view.load(pdf)
            self._set_view(view, widget)
        else:
            view.replace(pdf)
        view.set_loading_more(not final)
        if damaged:
            self._add_note("damaged", DAMAGED_NOTE)
        self._restore()
        self._thumbnail(pdf=pdf)

    def _restore(self) -> None:
        """Go back to the saved page and zoom once that page exists (11.2)."""
        pos, view = self._saved, self.view
        if pos is None or self._restored or not isinstance(view, PageView) or pos.page >= view.page_count:
            return
        self._restored = True
        if abs(pos.zoom - 1.0) > 1e-3:
            view.set_zoom(pos.zoom)
        if pos.page:
            view.go_to(pos.page)

    # Other views

    def _show_workbook(self, book, formula) -> None:
        view = self.view if isinstance(self.view, SheetView) else SheetView()
        view.load(book, formula)
        view.set_loading(False)
        self._set_view(view, view)

    def _sheet_unformatted(self, note: str) -> None:
        """The engine could not prepare the workbook: its preview stays, with a note (4.6)."""
        if isinstance(self.view, SheetView):
            self.view.set_formatting(False)
        self._add_note("unformatted", note)

    def _show_text(self, note: bool) -> None:
        """Text in its encoding; a file of no known format says it is shown as text (2.9, 7.1)."""
        view = TextView(page_bytes=self.context.text_page_bytes)
        try:
            if self.source.data is not None:
                view.load_bytes(self.source.data, self.source.name)
            else:
                view.load_path(self.source.path)
        except OSError as exc:
            self._fail(exc)
            return
        if note:
            view.show_note(SHOWN_AS_TEXT)
        self._set_view(view, view)

    def _show_bytes(self, kind: Kind, data: bytes) -> None:
        if kind is Kind.IMAGE:
            view = ImageView()
            view.load_bytes(data)
        else:
            from sovander.views.web_view import WebView

            view = WebView()
            view.connect("link-activated", weak_handler(self._on_web_link))
            view.connect("error", weak_handler(self._on_web_error))
            view.set_night(self.context.night)
            if kind is Kind.EPUB:
                pos = self._saved
                if pos is not None:
                    view.restore_position(f"{pos.page}.{int(pos.offset_y)}")
                    view.set_text_scale(pos.zoom)
                view.load_epub(data, self.source.name)
            else:
                view.load_markdown(data.decode(detect_encoding(data[: 1 << 20]), "replace"), self.source.name)
        self._set_view(view, view)

    def _show_archive(self, arc) -> None:
        """A zip as a folder; an entry opens through this pipeline from memory (8.1-8.3)."""
        try:
            from sovander.views.archive_view import ArchiveView
        except ImportError:
            arc.close()
            self._show_error(lambda: _("This format is not supported"))
            return
        view = ArchiveView(arc, weak_handler(self._open_entry), name=self.source.name)  # it closes arc
        self._set_view(view, view)

    def _open_entry(self, name: str, data: bytes) -> None:
        if self.on_open_entry is not None:
            self.on_open_entry(name, data)

    def _on_web_link(self, _view, uri: str) -> None:
        from sovander.links import confirm_and_open

        confirm_and_open(self.get_root(), uri)

    def _on_web_error(self, _view, message: str) -> None:
        self._add_note("web", lambda: message)

    def _view_as_text(self, *_args) -> None:
        self._show_text(note=True)

    # State pages

    def _set_view(self, view: Gtk.Widget, widget: Gtk.Widget, ready: bool = True) -> None:
        changed = view is not self.view
        self.view, self._rebuild = view, None
        if changed:
            self._backend = None
            self._replace(widget)
        if ready:
            self.state = "ready"
            self._report_first_page()
        if changed:
            self.emit("view-changed")

    def _show_state(self, state: str, build: Callable[[], Gtk.Widget]) -> None:
        had_view = self.view is not None
        self.view, self._layer, self._backend = None, None, None
        self.state, self._rebuild = state, build
        self._replace(build())
        if had_view:
            self.emit("view-changed")

    def _replace(self, widget: Gtk.Widget) -> None:
        child = self._slot.get_first_child()
        if child is not None:
            self._slot.remove(child)
        widget.set_vexpand(True)
        self._slot.append(widget)

    def _show_loading(self) -> None:
        self._show_state("loading", status_pages.loading_page)

    def _show_error(self, reason: Callable[[], str]) -> None:
        def build():
            page, self.error_text = status_pages.error_page(self.source.name, reason())
            return page

        self._show_state("error", build)

    def _show_install(self) -> None:
        def build():
            page, self.error_text = status_pages.install_page()
            return page

        self._show_state("error", build)

    def _show_unsupported(self) -> None:
        self._show_state("unsupported", lambda: status_pages.unsupported_page(
            self.source.name, weak_handler(self._view_as_text)))

    def _ask_password(self, wrong: bool) -> None:
        self.wrong_password = wrong

        def build():
            page, self.password_entry = status_pages.password_page(wrong, weak_handler(self.submit_password))
            return page

        self._show_state("password", build)
        self.password_entry.grab_focus()

    def submit_password(self, *_args) -> None:
        if self.password_entry is not None:
            self._password = self.password_entry.get_text()
            self._open()

    # Notes and failures

    def _add_note(self, key: str, message: str | Callable[[], str]) -> None:
        """A note above the view (10.2, 3.10); ``message`` is an N_() string or a callable."""
        text = message if callable(message) else functools.partial(_, message)
        if key in self._notes:
            self._notes[key][1].set_text(text())
            return
        bar, label = status_pages.note_bar(text())
        self._notes[key] = (text, label)
        self._notes_box.append(bar)

    @property
    def notes(self) -> list[str]:
        """The notes as shown."""
        return [label.get_text() for _text, label in self._notes.values()]

    def _fail(self, exc: BaseException) -> None:
        """Show why the document cannot be shown; the app and other windows go on (1.6, 10.5)."""
        if isinstance(exc, client.NeedsPassword | PasswordRequired):
            wrong = isinstance(exc, client.WrongPassword) or (
                isinstance(exc, PasswordRequired) and self._password is not None)
            self._ask_password(wrong)
            return
        if isinstance(self.view, PageView) and self.view.page_count:
            # page 1 is on screen and stays: the rest could not be prepared
            self.view.set_loading_more(False)
            self._add_note("failed", reason_for(exc))
            return
        if isinstance(exc, EngineUnavailable) and engine_missing():
            self._show_install()
        else:
            self._show_error(reason_for(exc))

    def _relabel(self, _language=None) -> None:
        for text, label in self._notes.values():
            label.set_text(text())
        if self._rebuild is not None:
            self._replace(self._rebuild())

    # Recents and positions (11.2)

    def _remember(self) -> None:
        store, src = self.context.store, self.source
        if store is None:
            return
        try:
            self._doc_key = doc_key(src.data if src.data is not None else src.path)
            self._saved = store.position(self._doc_key) if self._doc_key else None
            if src.path is not None:
                uri = src.path.absolute().as_uri()
                if store.record_open(uri, src.name, self.kind.value, self._doc_key) is not None:
                    self._thumb_uri = uri
                if self.kind in (Kind.PDF, Kind.IMAGE):
                    self._thumbnail(path=src.path)
        except OSError as exc:
            log.warning("could not record %s in the recents: %s", src.name, exc)

    def _thumbnail(self, **what) -> None:
        """The home screen's thumbnail of this file, drawn from the file or from page 1 (11.1)."""
        if self._thumb_uri is not None:
            from sovander import thumbs

            thumbs.for_store(self.context.store).remember(self._thumb_uri, self.kind.value, **what)
            self._thumb_uri = None

    def save_position(self) -> None:
        """Remember where the reader is, for the next time this document opens."""
        store, key, view = self.context.store, self._doc_key, self.view
        if store is None or key is None:
            return
        try:
            if isinstance(view, PageView) and view.page_count:
                store.save_position(key, view.current_page, 0.0, view.zoom)
            elif self.kind is Kind.EPUB and getattr(view, "position", None):
                chapter, _dot, letters = view.position.partition(".")
                store.save_position(key, int(chapter), float(letters), view.text_scale)
        except (OSError, ValueError) as exc:
            log.warning("could not save the position in %s: %s", self.source.name, exc)

    def close(self) -> None:
        """Save the position and drop whatever the workers still deliver."""
        self.save_position()
        self._token += 1

    def find_backend(self):
        """The search backend for the view shown (Requirement 6), or None."""
        from sovander import find

        view = self.view
        if self._backend is None and view is not None:
            if isinstance(view, PageView):
                self._backend = find.PageViewBackend(view, self._layer)
            elif isinstance(view, TextView):
                self._backend = find.TextViewBackend(view)
            elif isinstance(view, SheetView):
                self._backend = view
            elif type(view).__name__ == "WebView":
                self._backend = WebFindBackend(view)
        return self._backend

    def _report_first_page(self) -> None:
        """Log when the first page is on screen, for timing a start (logging level INFO)."""
        if self._reported or not log.isEnabledFor(logging.INFO):
            return
        self._reported = True
        ref = weakref.ref(self)

        def check():
            page = ref()
            if page is None or time.monotonic() - page._began > 120:
                return GLib.SOURCE_REMOVE
            view = page.view
            if isinstance(view, PageView) and not view.is_page_rendered(view.current_page):
                return GLib.SOURCE_CONTINUE
            log.info("%s: first page drawn %.2f s after opening began (at %.3f)",
                     page.source.name, time.monotonic() - page._began, time.time())
            return GLib.SOURCE_REMOVE

        GLib.timeout_add(10, check)


class WebFindBackend:
    """sovander.find's backend protocol over the web view's own search, which answers by signal."""

    def __init__(self, view):
        from sovander.views.selection import keep_with

        self.view, self.busy, self._listener = view, False, None
        view.connect("found", weak_handler(self._on_found))
        keep_with(view, self)

    def watch(self, listener) -> None:
        self._listener = listener

    def find(self, query: str, forward: bool = True, case: bool = False) -> tuple[int, int]:
        index, total = self.view.find(query, forward, case)
        self.busy = bool(query)
        return index - 1, total

    def clear(self) -> None:
        self.busy = False
        self.view.find_clear()

    def _on_found(self, _view, index: int, total: int, finished: bool) -> None:
        self.busy = not finished
        if self._listener is not None:
            self._listener(index - 1, total, self.busy)
