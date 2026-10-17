"""Text selection and copy on the page view (Requirement 3.5), and the layer that draws highlights.

Selection works on whole words from ``page.get_text("words")``, in the order MuPDF reads them (the
content stream order, which is reading order for the PDFs the engine writes). It attaches to a
PageView with its own controllers, so page_view.py needs no hooks. Highlights, for the selection and
for find, are drawn by a HighlightLayer that sits over the view's scrolled window in a Gtk.Overlay::

    scrolled = Gtk.ScrolledWindow(child=view)
    layer = HighlightLayer(view)
    box.append(layer.wrap(scrolled))
    selection = Selection(view, layer)
"""

from __future__ import annotations

from collections import OrderedDict
from collections.abc import Callable, Iterable

import gi

from sovander.views import weak_handler
from sovander.views.page_view import PageView

gi.require_version("Gdk", "4.0")
gi.require_version("Graphene", "1.0")
gi.require_version("Gtk", "4.0")
from gi.repository import Gdk, GLib, GObject, Graphene, Gtk  # noqa: E402

__all__ = ["HighlightLayer", "Selection", "keep_with", "page_point", "rgba", "visible_pages"]

WORD_CACHE = 32  # pages of words kept; a drag rarely spans more
EDGE = 24  # pixels from the top or bottom where a drag scrolls the view
SCROLL_MS = 30

Rect = tuple[float, float, float, float]
Painter = Callable[[int], Iterable[tuple[Gdk.RGBA, Rect]]]


def rgba(spec: str) -> Gdk.RGBA:
    """A colour from CSS text (Gdk.RGBA ignores constructor arguments)."""
    color = Gdk.RGBA()
    color.parse(spec)
    return color


SELECTED = rgba("rgba(53,132,228,0.38)")


def keep_with(widget: Gtk.Widget, helper) -> None:
    """Keep ``helper`` alive exactly as long as ``widget``.

    Helpers connect through weak_handler, so GTK does not keep them; the widget's Python object does,
    and that reference, unlike one from C, does not stop a closed view from being freed.
    """
    widget.__dict__.setdefault("_sovander_helpers", []).append(helper)


def _box(view: PageView, page: int) -> Graphene.Rect:
    w, h = view.page_size(page)
    return view.page_rect_to_widget(page, (0, 0, w, h))


def visible_pages(view: PageView) -> range:
    """Pages at least partly inside the view."""
    count, height = view.page_count, view.get_height()
    if count <= 0 or height <= 0:
        return range(0)
    first = last = min(max(0, view.current_page), count - 1)
    while first > 0 and (r := _box(view, first - 1)).get_y() + r.get_height() > 0:
        first -= 1
    while last + 1 < count and _box(view, last + 1).get_y() < height:
        last += 1
    return range(first, last + 1)


def page_point(view: PageView, x: float, y: float):
    """(page, x, y in points) under a widget point; off the pages, the nearest point of the nearest page."""
    hit = view.widget_point_to_page(x, y)
    if hit is not None:
        page, point = hit
        return page, point.x, point.y
    best = None
    for page in visible_pages(view) or range(min(1, view.page_count)):
        r = _box(view, page)
        top, bottom = r.get_y(), r.get_y() + r.get_height()
        distance = max(0.0, top - y, y - bottom)
        if best is None or distance < best[0]:
            best = (distance, page, r)
    if best is None:
        return None
    _d, page, r = best
    w, h = view.page_size(page)
    fx = min(max((x - r.get_x()) / max(r.get_width(), 1e-6), 0.0), 1.0)
    fy = min(max((y - r.get_y()) / max(r.get_height(), 1e-6), 0.0), 1.0)
    return page, fx * w, fy * h


class HighlightLayer(Gtk.Widget):
    """A see-through layer over a PageView that paints rectangles given in page points.

    Painters are ``painter(page) -> [(Gdk.RGBA, (x0, y0, x1, y1)), ...]``, asked for visible pages only.
    """

    __gtype_name__ = "SovanderHighlightLayer"

    def __init__(self, view: PageView):
        super().__init__(can_target=False, can_focus=False)
        self.view = view
        self._painters: list[Painter] = []
        self._watched: list[tuple[GObject.Object, int]] = []
        for name in ("hadjustment", "vadjustment"):
            view.connect(f"notify::{name}", weak_handler(self._watch_adjustments))
        for name in ("zoom", "page-count"):
            view.connect(f"notify::{name}", weak_handler(self._redraw))
        self._watch_adjustments()

    def wrap(self, scrolled: Gtk.Widget) -> Gtk.Overlay:
        """A Gtk.Overlay showing ``scrolled`` (the view's scrolled window) with this layer on top."""
        overlay = Gtk.Overlay(child=scrolled)
        overlay.add_overlay(self)
        return overlay

    def add_painter(self, painter: Painter) -> None:
        self._painters.append(painter)
        self.queue_draw()

    def remove_painter(self, painter: Painter) -> None:
        self._painters = [p for p in self._painters if p != painter]
        self.queue_draw()

    def _watch_adjustments(self, *_args):
        for adj, handler in self._watched:
            adj.disconnect(handler)
        self._watched = []
        for adj in (self.view.props.hadjustment, self.view.props.vadjustment):
            if adj is not None:
                for signal in ("value-changed", "changed"):
                    self._watched.append((adj, adj.connect(signal, weak_handler(self._redraw))))
        self.queue_draw()

    def _redraw(self, *_args):
        self.queue_draw()

    def do_measure(self, _orientation, _for_size):
        return 0, 0, -1, -1

    def do_snapshot(self, snap):
        if not self._painters:
            return
        ok, bounds = self.view.compute_bounds(self)
        if not ok:
            return
        ox, oy = bounds.get_x(), bounds.get_y()
        snap.push_clip(bounds)
        for page in visible_pages(self.view):
            for painter in self._painters:
                for color, rect in painter(page):
                    r = self.view.page_rect_to_widget(page, rect)
                    where = Graphene.Rect().init(
                        r.get_x() + ox, r.get_y() + oy, r.get_width(), r.get_height()
                    )
                    snap.append_color(color, where)
        snap.pop()


def nearest_word(words, x: float, y: float) -> int | None:
    """Index of the word at (x, y) in points, else the nearest one, preferring the same line.

    Above all text means the first word and below all text the last, so a drag past the end of
    a page takes the whole last line.
    """
    if not words:
        return None
    if y < min(w[1] for w in words):
        return 0
    if y > max(w[3] for w in words):
        return len(words) - 1
    best, best_key = None, None
    for i, (x0, y0, x1, y1, *_rest) in enumerate(words):
        key = (max(0.0, y0 - y, y - y1), max(0.0, x0 - x, x - x1))
        if best_key is None or key < best_key:
            best, best_key = i, key
    return best


def join_words(words) -> str:
    """Words as text: a space inside a line, a line break between lines."""
    parts, last_line = [], None
    for w in words:
        line = (w[5], w[6])
        if last_line is not None:
            parts.append(" " if line == last_line else "\n")
        parts.append(w[4])
        last_line = line
    return "".join(parts)


def line_rects(words) -> list[Rect]:
    """One rectangle per line of consecutive ``words``."""
    rects, last_line = [], None
    for x0, y0, x1, y1, _text, block, line, *_rest in words:
        if rects and (block, line) == last_line:
            a0, b0, a1, b1 = rects[-1]
            rects[-1] = (min(a0, x0), min(b0, y0), max(a1, x1), max(b1, y1))
        else:
            rects.append((x0, y0, x1, y1))
        last_line = (block, line)
    return rects


class Selection(GObject.Object):
    """Word selection on a PageView: drag, double-click a word, triple-click a line, Ctrl+A, Ctrl+C.

    Positions are (page, word index); emits ``changed`` when the selection changes.
    """

    __gtype_name__ = "SovanderSelection"
    __gsignals__ = {"changed": (GObject.SignalFlags.RUN_FIRST, None, ())}

    def __init__(self, view: PageView, layer: HighlightLayer | None = None):
        super().__init__()
        self.view, self.layer = view, layer
        self._doc, self._words = None, OrderedDict()
        self._anchor = self._focus = None
        self._drag = None  # [start x, start y, selecting, extend]
        self._pointer, self._scroll_id = None, 0
        if layer is not None:
            layer.add_painter(self._paint)
        keep_with(view, self)
        drag = Gtk.GestureDrag(button=Gdk.BUTTON_PRIMARY)
        drag.connect("drag-begin", weak_handler(self._on_drag_begin))
        drag.connect("drag-update", weak_handler(self._on_drag_update))
        drag.connect("drag-end", weak_handler(self._on_drag_end))
        click = Gtk.GestureClick(button=Gdk.BUTTON_PRIMARY)
        click.connect("pressed", weak_handler(self._on_pressed))
        click.connect("released", weak_handler(self._on_released))
        keys = Gtk.EventControllerKey()
        keys.connect("key-pressed", weak_handler(self._on_key))
        for controller in (drag, click, keys):
            view.add_controller(controller)

    # What is selected

    @property
    def empty(self) -> bool:
        self._check_document()
        return self._anchor is None

    @property
    def span(self):
        """((page, word), (page, word)) in reading order, both ends included, or None."""
        self._check_document()
        if self._anchor is None:
            return None
        return (self._anchor, self._focus) if self._anchor <= self._focus else (self._focus, self._anchor)

    def words(self, page: int) -> list:
        """The words of ``page`` as ``get_text("words")`` tuples, cached."""
        self._check_document()
        if page in self._words:
            self._words.move_to_end(page)
            return self._words[page]
        doc = self.view.document
        if doc is None or not 0 <= page < self.view.page_count:
            return []
        with self.view.lock:
            words = [] if doc.is_closed else doc[page].get_text("words")
        self._words[page] = words
        while len(self._words) > WORD_CACHE:
            self._words.popitem(last=False)
        return words

    def selected_words(self, page: int) -> list:
        span = self.span
        if span is None or not span[0][0] <= page <= span[1][0]:
            return []
        words = self.words(page)
        start = span[0][1] if page == span[0][0] else 0
        end = span[1][1] if page == span[1][0] else len(words) - 1
        return words[start : end + 1]

    @property
    def text(self) -> str:
        span = self.span
        if span is None:
            return ""
        pages = (join_words(self.selected_words(p)) for p in range(span[0][0], span[1][0] + 1))
        return "\n".join(p for p in pages if p)

    # Changing it

    def select(self, start: tuple[int, int], end: tuple[int, int]) -> None:
        """Select from word ``start`` to word ``end``, both (page, index), in either order."""
        self._check_document()
        if (start, end) != (self._anchor, self._focus):
            self._anchor, self._focus = start, end
            self._changed()

    def clear(self) -> None:
        self._check_document()
        if self._anchor is not None:
            self._anchor = self._focus = None
            self._changed()

    def select_page(self, page: int | None = None) -> bool:
        """Select every word of ``page`` (default: the current page); False when it has none."""
        page = self.view.current_page if page is None else page
        words = self.words(page)
        if not words:
            self.clear()
            return False
        self.select((page, 0), (page, len(words) - 1))
        self._to_primary()
        return True

    def select_word_at(self, x: float, y: float) -> bool:
        """Select the word under a widget point (double-click)."""
        hit = self._word_under(x, y)
        if hit is None:
            return False
        self.select(hit, hit)
        self._to_primary()
        return True

    def select_line_at(self, x: float, y: float) -> bool:
        """Select the line under a widget point (triple-click)."""
        hit = self._word_under(x, y)
        if hit is None:
            return False
        page, index = hit
        words = self.words(page)
        line = words[index][5:7]
        same = [i for i, w in enumerate(words) if w[5:7] == line]
        self.select((page, same[0]), (page, same[-1]))
        self._to_primary()
        return True

    def position_at(self, x: float, y: float):
        """(page, word) a drag to the widget point (x, y) reaches, or None on a page without text."""
        spot = page_point(self.view, x, y)
        if spot is None:
            return None
        page, px, py = spot
        index = nearest_word(self.words(page), px, py)
        return None if index is None else (page, index)

    def begin_drag(self, x: float, y: float, extend: bool = False) -> None:
        """A press at a widget point; ``extend`` (Shift) keeps the old start of the selection."""
        self._drag = [x, y, False, extend and self._anchor is not None]

    def update_drag(self, x: float, y: float) -> None:
        """The pointer moved to (x, y) with the button held."""
        if self._drag is None:
            return
        self._pointer = (x, y)
        sx, sy, selecting, extend = self._drag
        if not selecting:
            threshold = Gtk.Settings.get_default().get_property("gtk-dnd-drag-threshold")
            if max(abs(x - sx), abs(y - sy)) < threshold:
                return
            anchor = self._anchor if extend else self.position_at(sx, sy)
            if anchor is None:
                return
            self._drag[2], self._anchor = True, anchor
        focus = self.position_at(x, y)
        if focus is not None:
            self.select(self._anchor, focus)
        self._autoscroll(y)

    def end_drag(self) -> None:
        if self._drag is not None and self._drag[2]:
            self._to_primary()
        self._drag, self._pointer = None, None
        self._stop_autoscroll()

    def copy(self) -> str:
        """Put the selected text on the clipboard and return it ("" when nothing is selected)."""
        text = self.text
        if text:
            self.view.get_clipboard().set(text)
        return text

    def handle_key(self, keyval, state=0) -> bool:
        """Ctrl+A selects the page and Ctrl+C copies; True when the key was used."""
        if not state & Gdk.ModifierType.CONTROL_MASK or state & Gdk.ModifierType.SHIFT_MASK:
            return False
        if keyval in (Gdk.KEY_a, Gdk.KEY_A):
            self.select_page()
            return True
        if keyval in (Gdk.KEY_c, Gdk.KEY_C, Gdk.KEY_Insert):
            return bool(self.copy())
        return False

    # Internals

    def _word_under(self, x, y):
        hit = self.view.widget_point_to_page(x, y)
        if hit is None:
            return None
        page, point = hit
        for i, (x0, y0, x1, y1, *_rest) in enumerate(self.words(page)):
            if x0 <= point.x <= x1 and y0 <= point.y <= y1:
                return page, i
        return None

    def _check_document(self):
        """A new document (load or replace) drops the cached words and the selection."""
        doc = self.view.document
        if doc is not self._doc:
            self._doc, self._words = doc, OrderedDict()
            if self._anchor is not None:
                self._anchor = self._focus = None
                self._drag = None
                self.emit("changed")
                self._redraw()

    def _changed(self):
        self.emit("changed")
        self._redraw()

    def _redraw(self):
        if self.layer is not None:
            self.layer.queue_draw()

    def _paint(self, page):
        return [(SELECTED, rect) for rect in line_rects(self.selected_words(page))]

    def _to_primary(self):
        text = self.text
        if text:
            self.view.get_primary_clipboard().set(text)

    def _autoscroll(self, y):
        height = self.view.get_height()
        if y < EDGE or y > height - EDGE:
            if not self._scroll_id:
                self._scroll_id = GLib.timeout_add(SCROLL_MS, weak_handler(self._scroll_tick))
        else:
            self._stop_autoscroll()

    def _scroll_tick(self):
        adj = self.view.props.vadjustment
        if self._drag is None or self._pointer is None or adj is None or not self.view.get_mapped():
            self._scroll_id = 0
            return GLib.SOURCE_REMOVE
        x, y = self._pointer
        height = self.view.get_height()
        step = (y - EDGE) if y < EDGE else (y - height + EDGE) if y > height - EDGE else 0
        if not step:
            self._scroll_id = 0
            return GLib.SOURCE_REMOVE
        adj.set_value(adj.get_value() + max(-80.0, min(80.0, step / 2 if abs(step) > 8 else step)))
        self.update_drag(x, y)
        return GLib.SOURCE_CONTINUE

    def _stop_autoscroll(self):
        if self._scroll_id:
            GLib.source_remove(self._scroll_id)
            self._scroll_id = 0

    def _on_drag_begin(self, gesture, x, y):
        shift = bool(gesture.get_current_event_state() & Gdk.ModifierType.SHIFT_MASK)
        self.begin_drag(x, y, extend=shift)

    def _on_drag_end(self, _gesture, _dx, _dy):
        self.end_drag()

    def _on_key(self, _controller, keyval, _code, state):
        return self.handle_key(keyval, state)

    def _on_drag_update(self, gesture, dx, dy):
        ok, x, y = gesture.get_start_point()
        if ok:
            self.update_drag(x + dx, y + dy)

    def _on_pressed(self, _gesture, n_press, x, y):
        if n_press == 2:
            self.select_word_at(x, y)
        elif n_press == 3:
            self.select_line_at(x, y)

    def _on_released(self, gesture, n_press, x, y):
        if n_press != 1 or (self._drag is not None and self._drag[2]):
            return
        if gesture.get_current_event_state() & Gdk.ModifierType.SHIFT_MASK and self._anchor is not None:
            focus = self.position_at(x, y)  # Shift+click extends the selection
            if focus is not None:
                self.select(self._anchor, focus)
                self._to_primary()
        else:
            self.clear()  # a plain click drops the selection
