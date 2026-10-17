"""Find in document (Requirement 6): one search bar over per-view backends.

A backend (FindBackend) has ``find(query, forward=True, case=False) -> (index, total)`` and ``clear()``.
``find`` with the previous query and case steps to the next or previous match; anything else starts
over from the reader's position. ``index`` is 0-based, -1 while no match is current. A backend that
counts in the background also has ``watch(listener)`` and calls ``listener(index, total, busy)`` on
the main thread. The web view and the sheet view plug in the same way.

Pages are not searched with ``page.search_for``: MuPDF ignores case only for Latin letters ("привет"
does not find "Привет"), so matching is done here on the page's characters.
"""

from __future__ import annotations

import bisect
import functools
import logging
import threading
import time
import weakref
from collections import OrderedDict
from typing import Protocol, runtime_checkable

import gi

from sovander import i18n
from sovander.i18n import _
from sovander.views import weak_handler
from sovander.views.selection import keep_with, rgba

try:
    import pymupdf as fitz
except ImportError:  # PyMuPDF before 1.24 (Ubuntu 22.04 and 24.04) has only the old name
    import fitz

gi.require_version("Gdk", "4.0")
gi.require_version("Gtk", "4.0")
from gi.repository import Gdk, GLib, Gtk  # noqa: E402

log = logging.getLogger(__name__)

__all__ = ["FindBackend", "FindBar", "PageViewBackend", "TextViewBackend", "fold", "page_hits"]

TEXT_FLAGS = getattr(fitz, "TEXT_MEDIABOX_CLIP", 0)  # ligatures split into letters, so "fi" finds "ﬁ"
RECT_CACHE = 16  # pages whose match rectangles are kept
BATCH_SECONDS = 0.1  # progress reaches the bar about this often
MAX_TAGGED = 10000  # text view: matches painted; the count is always complete


MATCH, CURRENT = rgba("rgba(255,213,0,0.45)"), rgba("rgba(255,120,0,0.6)")


@runtime_checkable
class FindBackend(Protocol):
    def find(self, query: str, forward: bool = True, case: bool = False) -> tuple[int, int]: ...

    def clear(self) -> None: ...


def fold(text: str) -> str:
    """Lower case that keeps every character in place, so offsets match the original text."""
    low = text.lower()
    if len(low) == len(text):  # lower() never shortens text, so equal length means 1:1
        return low
    return "".join(c.lower() if len(c.lower()) == 1 else c for c in text)


def _union(a, b):
    return min(a[0], b[0]), min(a[1], b[1]), max(a[2], b[2]), max(a[3], b[3])


def _occurrences(hay: str, needle: str):
    i = hay.find(needle)
    while i >= 0:
        yield i
        i = hay.find(needle, i + len(needle))


def _stream(rawdict):
    """The page's characters with lines joined by single spaces, plus each one's box and line."""
    chars, boxes, lines = [], [], []
    text_lines = (line for block in rawdict["blocks"] if block.get("type", 0) == 0 for line in block["lines"])
    for line_no, line in enumerate(text_lines):
        cells = [(c, ch["bbox"]) for span in line["spans"] for ch in span["chars"] for c in ch["c"]]
        for c, box in [(" ", None), *cells]:  # a space between lines
            if c.isspace():
                if not chars or chars[-1] == " ":
                    continue
                c, box = " ", None
            chars.append(c)
            boxes.append(box)
            lines.append(line_no)
    return "".join(chars), boxes, lines


def page_hits(page, query: str, case: bool = False) -> list[list[tuple]]:
    """Matches of ``query`` on a fitz page: one list of line rectangles (points) per match.

    Spaces in the query match any run of spaces or a line break. Call under the fitz lock.
    """
    needle = " ".join(query.split())
    if not needle:
        return []
    if not case:
        needle = fold(needle)
    textpage = page.get_textpage(flags=TEXT_FLAGS)
    plain = " ".join(page.get_text("text", textpage=textpage).split())
    if needle not in (plain if case else fold(plain)):
        return []
    text, boxes, lines = _stream(page.get_text("rawdict", textpage=textpage))
    hits = []
    for start in _occurrences(text if case else fold(text), needle):
        per_line = {}
        for k in range(start, start + len(needle)):
            box = boxes[k]
            if box is not None:
                old = per_line.get(lines[k])
                per_line[lines[k]] = box if old is None else _union(old, box)
        hits.append(list(per_line.values()))
    return hits


def _reveal(view, rect):
    """Scroll a widget so ``rect`` (widget coordinates) is inside it, a third from the top."""
    for adj, start, size, length in (
        (view.props.vadjustment, rect.get_y(), rect.get_height(), view.get_height()),
        (view.props.hadjustment, rect.get_x(), rect.get_width(), view.get_width()),
    ):
        if adj is not None and (start < 0 or start + size > length):
            adj.set_value(adj.get_value() + start - min(length / 3, max(0.0, length - size) / 2))


def _search_pages(ref, gen, doc, lock, needle, case, count):
    """Worker thread: count matches page by page. It holds the backend weakly, so a closed view stops it."""
    batch, last = {}, time.monotonic()
    for page in range(count):
        backend = ref()
        if backend is None or backend._gen != gen:
            return
        del backend
        try:
            with lock:
                if doc.is_closed:
                    return
                found = len(page_hits(doc[page], needle, case))
        except Exception as exc:  # noqa: BLE001 - a damaged page must not stop the search
            log.warning("page %d could not be searched: %s", page + 1, exc)
            found = 0
        if found:
            batch[page] = found
        if time.monotonic() - last > BATCH_SECONDS:
            GLib.idle_add(_deliver, ref, gen, batch, False)
            batch, last = {}, time.monotonic()
    GLib.idle_add(_deliver, ref, gen, batch, True)


def _deliver(ref, gen, batch, done):
    backend = ref()
    if backend is not None and backend._gen == gen:
        backend._take(batch, done)
    return GLib.SOURCE_REMOVE


class PageViewBackend:
    """Search of every page of a PageView on a worker thread, highlights on a HighlightLayer."""

    def __init__(self, view, layer=None):
        self.view, self.layer = view, layer
        self.busy = False
        self._key, self._gen, self._listener = None, 0, None
        self._counts, self._pages = {}, []
        self._current, self._start = None, 0
        self._rects = OrderedDict()
        if layer is not None:
            layer.add_painter(self._paint)
        keep_with(view, self)

    def watch(self, listener) -> None:
        self._listener = listener

    @property
    def total(self) -> int:
        return sum(self._counts.values())

    @property
    def index(self) -> int:
        if self._current is None:
            return -1
        page, i = self._current
        return sum(self._counts[p] for p in self._pages[: bisect.bisect_left(self._pages, page)]) + i

    def find(self, query: str, forward: bool = True, case: bool = False) -> tuple[int, int]:
        needle = " ".join(query.split())
        if not needle:
            self.clear()
            return -1, 0
        key = (needle, bool(case), self.view.document)
        if key != self._key:
            self._restart(key)
        elif self._current is not None:
            self._step(forward)
            self._show()
        return self.index, self.total

    def clear(self) -> None:
        self._gen += 1
        self._key, self.busy, self._current = None, False, None
        self._counts, self._pages, self._rects = {}, [], OrderedDict()
        self._redraw()

    def hits(self, page: int) -> list[list[tuple]]:
        """Match rectangles of ``page`` for the current search, computed when first needed."""
        if self._key is None or page not in self._counts or self.view.document is not self._key[2]:
            return []
        if page not in self._rects:
            needle, case, doc = self._key
            with self.view.lock:
                self._rects[page] = [] if doc.is_closed else page_hits(doc[page], needle, case)
            while len(self._rects) > RECT_CACHE:
                self._rects.popitem(last=False)
        self._rects.move_to_end(page)
        return self._rects[page]

    def _restart(self, key):
        self.clear()
        self._key, doc = key, key[2]
        self._start = self.view.current_page
        if doc is None:
            self._notify()
            return
        self.busy = True
        args = (weakref.ref(self), self._gen, doc, self.view.lock, *key[:2], self.view.page_count)
        threading.Thread(target=_search_pages, args=args, name="sovander-find", daemon=True).start()
        self._notify()

    def _take(self, batch, done):
        """Counts from the worker, on the main thread."""
        self._counts.update(batch)
        self._pages = sorted(self._counts)
        self.busy = not done
        if self._current is None:
            after = [p for p in sorted(batch) if p >= self._start]
            if after or (done and self._pages):
                self._current = ((after or self._pages)[0], 0)
                self._show()
        self._redraw()
        self._notify()

    def _step(self, forward):
        page, i = self._current
        if forward:
            if i + 1 < self._counts[page]:
                self._current = (page, i + 1)
                return
            k = bisect.bisect_right(self._pages, page)
            if k < len(self._pages) or not self.busy:  # wrap only once every page is searched
                self._current = (self._pages[k % len(self._pages)], 0)
        else:
            if i > 0:
                self._current = (page, i - 1)
                return
            k = bisect.bisect_left(self._pages, page) - 1
            if k >= 0 or not self.busy:
                target = self._pages[k]
                self._current = (target, self._counts[target] - 1)

    def _show(self):
        page, i = self._current
        hits = self.hits(page)
        if i < len(hits) and hits[i]:
            _reveal(self.view, self.view.page_rect_to_widget(page, functools.reduce(_union, hits[i])))
        else:
            self.view.go_to(page)
        self._redraw()

    def _paint(self, page):
        hits = self.hits(page)
        current = self._current[1] if self._current and self._current[0] == page else -1
        return [(CURRENT if n == current else MATCH, rect) for n, hit in enumerate(hits) for rect in hit]

    def _redraw(self):
        if self.layer is not None:
            self.layer.queue_draw()

    def _notify(self):
        if self._listener is not None:
            self._listener(self.index, self.total, self.busy)


class TextViewBackend:
    """Search of a Gtk.TextView's text (a sovander TextView or a plain Gtk.TextView)."""

    def __init__(self, text_view):
        self.view = getattr(text_view, "view", text_view)
        self.buffer = self.view.get_buffer()
        self._tag = Gtk.TextTag(background_rgba=MATCH)
        self.buffer.get_tag_table().add(self._tag)
        self._mark = self.buffer.create_mark(None, self.buffer.get_start_iter(), True)
        self._key, self._matches, self._current = None, [], -1
        # More text (Show more) means counting again on the next find, from the same match
        self.buffer.connect("changed", weak_handler(self._on_changed))
        keep_with(self.view, self)

    def _on_changed(self, _buffer):
        self._key = None

    def find(self, query: str, forward: bool = True, case: bool = False) -> tuple[int, int]:
        if not query:
            self.clear()
            return -1, 0
        key = (query, bool(case))
        if key != self._key:
            near = self._matches[self._current][0] if self._current >= 0 else self._top_offset()
            self._search(query, case)
            self._key = key
            self._current = bisect.bisect_left(self._matches, (near,)) if self._matches else -1
            if self._current >= len(self._matches):
                self._current = 0
        elif self._matches:
            self._current = (self._current + (1 if forward else -1)) % len(self._matches)
        if self._current >= 0:
            self._show()
        return self._current, len(self._matches)

    def clear(self) -> None:
        self.buffer.remove_tag(self._tag, self.buffer.get_start_iter(), self.buffer.get_end_iter())
        self._key, self._matches, self._current = None, [], -1

    def _top_offset(self) -> int:
        rect = self.view.get_visible_rect()
        found, it = self.view.get_iter_at_location(rect.x, rect.y)
        return it.get_offset() if found and self.view.get_mapped() else 0

    def _search(self, query, case):
        buffer = self.buffer
        buffer.remove_tag(self._tag, buffer.get_start_iter(), buffer.get_end_iter())
        text = buffer.get_text(buffer.get_start_iter(), buffer.get_end_iter(), True)
        needle = query if case else fold(query)
        starts = _occurrences(text if case else fold(text), needle)
        self._matches = [(s, s + len(needle)) for s in starts]
        for start, end in self._matches[:MAX_TAGGED]:
            buffer.apply_tag(self._tag, buffer.get_iter_at_offset(start), buffer.get_iter_at_offset(end))

    def _show(self):
        start, end = self._matches[self._current]
        first, last = self.buffer.get_iter_at_offset(start), self.buffer.get_iter_at_offset(end)
        self.buffer.select_range(first, last)
        self.buffer.move_mark(self._mark, first)
        self.view.scroll_to_mark(self._mark, 0.1, True, 0.0, 0.3)


class FindBar(Gtk.SearchBar):
    """The search bar: Ctrl+F opens it, Escape closes it, Enter and Shift+Enter step (Requirement 6)."""

    __gtype_name__ = "SovanderFindBar"

    def __init__(self, backend: FindBackend | None = None):
        super().__init__(show_close_button=True)
        self._backend, self._busy, self._return_focus = None, False, None
        self.result = (-1, 0)  # (index, total) as last reported; index 0-based, -1 for none
        self.entry = Gtk.SearchEntry(hexpand=True)
        self.previous_button = Gtk.Button(icon_name="go-up-symbolic", sensitive=False)
        self.next_button = Gtk.Button(icon_name="go-down-symbolic", sensitive=False)
        self.case_button = Gtk.CheckButton()
        self.spinner = Gtk.Spinner(visible=False)
        self.status = Gtk.Label(xalign=0, width_chars=10)
        box = Gtk.Box(spacing=6)
        for child in (self.entry, self.previous_button, self.next_button, self.case_button):
            box.append(child)
        box.append(self.spinner)
        box.append(self.status)
        self.set_child(box)
        self.connect_entry(self.entry)
        for widget, signal, method in (
            (self.entry, "search-changed", self.search),
            (self.entry, "activate", self.next),
            (self.entry, "next-match", self.next),
            (self.entry, "previous-match", self.previous),
            (self.previous_button, "clicked", self.previous),
            (self.next_button, "clicked", self.next),
            (self.case_button, "toggled", self.search),
            (self, "notify::search-mode-enabled", self._on_mode),
        ):
            widget.connect(signal, weak_handler(method))
        keys = Gtk.EventControllerKey(propagation_phase=Gtk.PropagationPhase.CAPTURE)
        keys.connect("key-pressed", weak_handler(self._on_entry_key))
        self.entry.add_controller(keys)
        i18n.add_listener(self._relabel)
        self._relabel()
        if backend is not None:
            self.set_backend(backend)

    def set_backend(self, backend: FindBackend | None) -> None:
        """Search a different view; the old one's highlights are cleared."""
        if self._backend is not None:
            self._backend.clear()
            if hasattr(self._backend, "watch"):
                self._backend.watch(None)
        self._backend = backend
        if backend is not None and hasattr(backend, "watch"):
            backend.watch(self._on_progress)
        self._update(-1, 0, False)
        if self.get_search_mode():
            self.search()

    def attach(self, window: Gtk.Widget) -> None:
        """Ctrl+F in ``window`` opens the bar; Escape anywhere in it closes the open bar."""
        shortcuts = Gtk.ShortcutController(propagation_phase=Gtk.PropagationPhase.CAPTURE)
        for trigger, method in (("<Control>f", self.open), ("Escape", self._shortcut_close)):
            action = Gtk.CallbackAction.new(weak_handler(method))
            shortcuts.add_shortcut(Gtk.Shortcut.new(Gtk.ShortcutTrigger.parse_string(trigger), action))
        window.add_controller(shortcuts)

    def open(self, *_args) -> bool:
        """Show the bar with the query selected, searching again if it has one (6.1); True."""
        root = self.get_root()
        focus = root.get_focus() if isinstance(root, Gtk.Root) else None
        if focus is not None and not focus.is_ancestor(self):
            self._return_focus = focus
        was_open = self.get_search_mode()
        self.set_search_mode(True)
        self.entry.grab_focus()
        self.entry.select_region(0, -1)
        if not was_open and self.entry.get_text():
            self.search()
        return True

    def close(self) -> None:
        self.set_search_mode(False)

    def search(self, *_args) -> None:
        """Search for the entry's text from the reader's position, or clear when it is empty (6.2)."""
        self._ask(True)

    def next(self, *_args) -> None:
        self._ask(True)

    def previous(self, *_args) -> None:
        self._ask(False)

    def _ask(self, forward):
        query = self.entry.get_text()
        if self._backend is None:
            return
        if not query.strip():
            self._backend.clear()
            self._update(-1, 0, False)
            return
        index, total = self._backend.find(query, forward, self.case_button.get_active())
        self._update(index, total, bool(getattr(self._backend, "busy", False)))

    def _on_progress(self, index, total, busy):
        if self.entry.get_text().strip():
            self._update(index, total, busy)

    def _update(self, index, total, busy):
        self.result, self._busy = (index, total), busy
        self.spinner.set_visible(busy)
        self.spinner.set_spinning(busy)
        text, query = "", self.entry.get_text().strip()
        if query and index >= 0:
            text = _("{current} of {total}").format(current=index + 1, total=total)
        elif query and total == 0 and not busy:
            text = _("No matches")
        self.status.set_text(text)
        none = bool(text) and index < 0
        (self.entry.add_css_class if none else self.entry.remove_css_class)("error")
        for button in (self.previous_button, self.next_button):
            button.set_sensitive(total > 0)

    def _relabel(self, _language=None):
        self.entry.set_property("placeholder-text", _("Find in document"))
        self.previous_button.set_tooltip_text(_("Previous match"))
        self.next_button.set_tooltip_text(_("Next match"))
        self.case_button.set_label(_("Match Case"))
        self._update(*self.result, self._busy)

    def _on_entry_key(self, _controller, keyval, _code, state):
        if keyval in (Gdk.KEY_Return, Gdk.KEY_KP_Enter, Gdk.KEY_ISO_Enter):
            (self.previous if state & Gdk.ModifierType.SHIFT_MASK else self.next)()
            return True
        return False

    def _shortcut_close(self, *_args):
        if not self.get_search_mode():
            return False
        self.close()
        return True

    def _on_mode(self, *_args):
        if self.get_search_mode():
            return
        if self._backend is not None:
            self._backend.clear()
        self._update(-1, 0, False)
        focus, self._return_focus = self._return_focus, None
        if focus is not None and focus.get_root() is self.get_root():
            focus.grab_focus()
