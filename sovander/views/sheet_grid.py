"""The sheet view's cell grid (Requirements 4.1-4.4): a virtualised, custom-drawn Gtk.Scrollable.

Like the page view, it sits in a Gtk.ScrolledWindow and each frame draws only the cells in view, so a
100,000-row sheet costs what one screen costs. Gtk.ColumnView was rejected: its rows are separate
widgets, so a merge cannot span rows, no column stays frozen, and every visible cell would be a widget
bound from Python on each scroll step. Text is drawn as the Grid holds it (the file's number formats),
never recalculated (4.2, 4.3). Names i, j are positions among shown rows and columns; row, col are
real sheet coordinates.
"""

from __future__ import annotations

import bisect
import logging
import re
import weakref
from collections import OrderedDict
from collections.abc import Callable
from itertools import accumulate, islice
from typing import Any

import gi

gi.require_version("Gdk", "4.0")
gi.require_version("Graphene", "1.0")
gi.require_version("Gsk", "4.0")
gi.require_version("Gtk", "4.0")
gi.require_version("Pango", "1.0")
from gi.repository import Gdk, GLib, GObject, Graphene, Gsk, Gtk, Pango  # noqa: E402

from sovander.views import weak_handler  # noqa: E402

__all__ = ["SheetGrid", "cell_address", "column_letters", "looks_numeric"]

log = logging.getLogger(__name__)

PAD = 3  # text inset inside a cell
FONT_POINTS = 10  # rows saved at the usual 17 px fit one line of 10 pt text
INDEX_CHUNK = 50_000  # cells scanned for merges per idle turn: about 5 ms on a real Grid
LAYOUT_CACHE = 6000
FORMULA_DELAY_MS = 150  # arrowing through cells must not queue one engine job per key press
DEFAULT_HEIGHT, DEFAULT_WIDTH = 17, 64

_NUMBER = re.compile(
    r"[(+\-−]?\s*[^\w\s]{0,3}\s*\d[\d\s.,:/'’  -]*(?:[eE][+-]?\d+)?\s*%?"
    r"\s*(?:[^\w\s]{1,3}|[^\W\d_]{1,4}\.?)?\)?"
)
_KEYPAD = {Gdk.KEY_KP_Right: Gdk.KEY_Right, Gdk.KEY_KP_Left: Gdk.KEY_Left, Gdk.KEY_KP_Down: Gdk.KEY_Down,
           Gdk.KEY_KP_Up: Gdk.KEY_Up, Gdk.KEY_KP_Home: Gdk.KEY_Home, Gdk.KEY_KP_End: Gdk.KEY_End,
           Gdk.KEY_KP_Page_Down: Gdk.KEY_Page_Down, Gdk.KEY_KP_Page_Up: Gdk.KEY_Page_Up}
_HAS_GET_COLOR = hasattr(Gtk.Widget, "get_color")  # GTK 4.10; the 4.6 floor has only the style context


def column_letters(col: int) -> str:
    """Column letters of a 0-based index: 0 → A, 25 → Z, 26 → AA."""
    letters = ""
    col += 1
    while col:
        col, rest = divmod(col - 1, 26)
        letters = chr(65 + rest) + letters
    return letters


def cell_address(row: int, col: int) -> str:
    return f"{column_letters(col)}{row + 1}"


def looks_numeric(text: str) -> bool:
    """True for text a spreadsheet would right-align: numbers, dates, money, percentages."""
    return bool(text) and _NUMBER.fullmatch(text.strip()) is not None


def _rgba(spec: str) -> Gdk.RGBA:
    color = Gdk.RGBA()
    color.parse(spec)
    return color


def _palette(spec: dict[str, str]) -> dict[str, Gdk.RGBA]:
    return {name: _rgba(value) for name, value in spec.items()}


_PALETTES = {  # keyed by "the theme is dark"; file fills are drawn as saved either way
    False: _palette({"bg": "#ffffff", "head": "#f2f2f2", "line": "rgba(0,0,0,0.12)",
                     "edge": "rgba(0,0,0,0.25)", "sel": "#1c71d8", "mark": "rgba(53,132,228,0.18)"}),
    True: _palette({"bg": "#1e1e1e", "head": "#2c2c2c", "line": "rgba(255,255,255,0.12)",
                    "edge": "rgba(255,255,255,0.25)", "sel": "#78aeed", "mark": "rgba(120,174,237,0.22)"}),
}
_MATCH, _CURRENT = _rgba("rgba(255,214,0,0.45)"), _rgba("rgba(255,120,0,0.6)")  # as sovander.find
_INK = {False: _rgba("rgba(0,0,0,0.87)"), True: _rgba("rgba(255,255,255,0.92)")}  # by "the fill is dark"
_FILLS: dict[str, tuple[Gdk.RGBA | None, bool]] = {}


def _fill(spec: str) -> tuple[Gdk.RGBA | None, bool]:
    """A file's fill colour and whether it is dark, parsed once per colour."""
    got = _FILLS.get(spec)
    if got is None:
        color = Gdk.RGBA()
        got = (color, _luma(color) < 0.5) if color.parse(spec) else (None, False)
        _FILLS[spec] = got
    return got


def _luma(c: Gdk.RGBA) -> float:
    return 0.299 * c.red + 0.587 * c.green + 0.114 * c.blue


def _rect(x: float, y: float, w: float, h: float) -> Graphene.Rect:
    return Graphene.Rect().init(x, y, max(0, w), max(0, h))


def _span(edges: list[int], a: float, b: float) -> tuple[int, int]:
    """First and last line whose [edges[k], edges[k + 1]) meets [a, b)."""
    last_line = len(edges) - 2
    first = min(last_line, max(0, bisect.bisect_right(edges, a) - 1))
    return first, min(last_line, max(first, bisect.bisect_left(edges, b) - 1))


def _position(lines: list[int], value: int) -> int | None:
    """Where a real row or column sits among the shown ones (sorted), or None if hidden."""
    k = bisect.bisect_left(lines, value)
    return k if k < len(lines) and lines[k] == value else None


def _sizes(values: list[int], n: int, default: int) -> list[int]:
    return [max(1, v) for v in values[:n]] + [default] * (n - len(values))


def _draw_text(snap: Gtk.Snapshot, layout: Pango.Layout, x: float, y: float, ink: Gdk.RGBA) -> None:
    snap.save()
    snap.translate(Graphene.Point().init(x, y))
    snap.append_layout(layout, ink)
    snap.restore()


class SheetGrid(Gtk.Widget, Gtk.Scrollable):
    """Cells with frozen column letters and row numbers; scrolls through its adjustments."""

    __gtype_name__ = "SovanderSheetGrid"
    hscroll_policy = GObject.Property(type=Gtk.ScrollablePolicy, default=Gtk.ScrollablePolicy.MINIMUM)
    vscroll_policy = GObject.Property(type=Gtk.ScrollablePolicy, default=Gtk.ScrollablePolicy.MINIMUM)

    def __init__(self, on_select: Callable[[], None]):
        self._hadj = self._vadj = None
        self._scroll_ids = {"h": 0, "v": 0}  # value-changed handlers, to disconnect a replaced adjustment
        super().__init__(focusable=True, overflow=Gtk.Overflow.HIDDEN)
        self._on_select = weakref.WeakMethod(on_select)
        self._layouts: OrderedDict[Any, tuple[Pango.Layout, int, int, bool]] = OrderedDict()
        self._font_name, self._index_id = "", 0
        self.hits: set[tuple[int, int]] = set()  # search matches, drawn like sovander.find's
        self.current_hit: tuple[int, int] | None = None
        keys = Gtk.EventControllerKey()
        keys.connect("key-pressed", weak_handler(self._on_key_pressed))
        click = Gtk.GestureClick()
        click.connect("pressed", weak_handler(self._on_click))
        self.add_controller(keys)
        self.add_controller(click)
        self.set_grid(None)

    @GObject.Property(type=Gtk.Adjustment)
    def hadjustment(self):
        return self._hadj

    @hadjustment.setter
    def hadjustment(self, adj):
        self._hadj = self._adopt("h", self._hadj, adj)

    @GObject.Property(type=Gtk.Adjustment)
    def vadjustment(self):
        return self._vadj

    @vadjustment.setter
    def vadjustment(self, adj):
        self._vadj = self._adopt("v", self._vadj, adj)

    def _adopt(self, axis, old, adj):
        if old is not None and self._scroll_ids[axis]:
            old.disconnect(self._scroll_ids[axis])
        self._scroll_ids[axis] = adj.connect("value-changed", weak_handler(self._on_scrolled)) if adj else 0
        return adj

    def _on_scrolled(self, _adj):
        self.queue_draw()

    # Grid and geometry

    def set_grid(self, grid) -> None:
        """Show a sheets.Grid (or nothing) from its top-left cell."""
        self.grid = grid
        rows, cols = (grid.rows, grid.cols) if grid is not None else ([], [])
        heights, widths = (grid.row_heights, grid.col_widths) if grid is not None else ([], [])
        self._ys = list(accumulate(_sizes(heights, len(rows), DEFAULT_HEIGHT), initial=0))
        self._xs = list(accumulate(_sizes(widths, len(cols), DEFAULT_WIDTH), initial=0))
        self.sel = (0, 0) if rows and cols else None
        self.hits, self.current_hit = set(), None
        self._layouts.clear()
        self._merges: list[tuple[int, int, int, int]] = []  # (i0, j0, i1, j1), ends included
        self._tops: list[int] = []
        self._tall, self._indexed = 1, False
        if self._index_id:
            GLib.source_remove(self._index_id)
        # The first paint needs no index (merges starting in view are met while drawing), so a huge
        # sheet's scan runs in idle turns after it, at a lower priority than redraws.
        self._pending = iter(grid.cells.items()) if grid is not None else iter(())
        self._index_id = GLib.idle_add(weak_handler(self._index_step))
        self._measure_headers()
        for adj in (self._hadj, self._vadj):
            if adj is not None:
                adj.set_value(0)
        self._configure()
        self.queue_draw()

    def _index_step(self) -> bool:
        """Collect merged cells, one chunk per call; True while cells remain."""
        rows, cols = (self.grid.rows, self.grid.cols) if self.grid is not None else ([], [])
        chunk = list(islice(self._pending, INDEX_CHUNK))
        for (row, col), cell in chunk:
            if cell.rowspan > 1 or cell.colspan > 1:
                i, j = _position(rows, row), _position(cols, col)
                if i is not None and j is not None:
                    i1, j1 = min(i + cell.rowspan, len(rows)) - 1, min(j + cell.colspan, len(cols)) - 1
                    self._merges.append((i, j, i1, j1))
        if len(chunk) == INDEX_CHUNK:
            return True
        self._merges.sort()
        self._tops = [m[0] for m in self._merges]
        self._tall = max((m[2] - m[0] + 1 for m in self._merges), default=1)
        self._indexed, self._index_id = True, 0
        self.queue_draw()
        return False

    def _merges_in(self, i0: int, i1: int, j0: int, j1: int) -> list[tuple[int, int, int, int]]:
        if self._indexed:
            lo = bisect.bisect_left(self._tops, i0 - self._tall + 1)
            found = self._merges[lo: bisect.bisect_right(self._tops, i1)]
        else:
            found = self._merges
        return [m for m in found if m[2] >= i0 and m[0] <= i1 and m[3] >= j0 and m[1] <= j1]

    def _box(self, i: int, j: int) -> tuple[int, int, int, int]:
        """The cell at (i, j) with its merge span, as (i0, j0, i1, j1)."""
        cell = self.grid.cells.get((self.grid.rows[i], self.grid.cols[j]))
        if cell is None or (cell.rowspan == 1 and cell.colspan == 1):
            return i, j, i, j
        last_i, last_j = len(self._ys) - 2, len(self._xs) - 2
        return i, j, min(i + cell.rowspan - 1, last_i), min(j + cell.colspan - 1, last_j)

    def _owner(self, i: int, j: int) -> tuple[int, int]:
        """The top-left cell of the merge covering (i, j), or (i, j) itself."""
        merge = next(iter(self._merges_in(i, i, j, j)), None)
        return (merge[0], merge[1]) if merge else (i, j)

    def row_label(self, i: int) -> str:
        return str(self.grid.rows[i] + 1)

    def column_label(self, j: int) -> str:
        return column_letters(self.grid.cols[j])

    def cell_rect(self, i: int, j: int) -> tuple[float, float, float, float]:
        """(x, y, width, height) of a cell or its merge, in widget coordinates."""
        i0, j0, i1, j1 = self._box(i, j)
        ox, oy = self._offset()
        x, y = self._hw + self._xs[j0] - ox, self._hh + self._ys[i0] - oy
        return x, y, self._xs[j1 + 1] - self._xs[j0], self._ys[i1 + 1] - self._ys[i0]

    def at(self, x: float, y: float) -> tuple[int, int] | None:
        """The cell (merge top-left) under a widget point, or None over headers and blank space."""
        if not self.sel or x < self._hw or y < self._hh:
            return None
        ox, oy = self._offset()
        cx, cy = x - self._hw + ox, y - self._hh + oy
        if cx >= self._xs[-1] or cy >= self._ys[-1]:
            return None
        return self._owner(bisect.bisect_right(self._ys, cy) - 1, bisect.bisect_right(self._xs, cx) - 1)

    def _offset(self) -> tuple[float, float]:
        return (self._hadj.get_value() if self._hadj else 0.0, self._vadj.get_value() if self._vadj else 0.0)

    def _configure(self) -> None:
        sizes = ((self._hadj, self.get_width() - self._hw, self._xs[-1]),
                 (self._vadj, self.get_height() - self._hh, self._ys[-1]))
        for adj, page, total in sizes:
            if adj is not None:
                page = max(0, page)
                value = min(adj.get_value(), max(0, total - page))
                adj.configure(value, 0, max(total, page), 40, page * 0.9, page)

    def _measure_headers(self) -> None:
        font = self.get_pango_context().get_font_description() or Pango.FontDescription.from_string("Sans")
        if font.to_string() != self._font_name:
            self._font_name = font.to_string()
            self._font = font.copy()
            self._font.set_size(FONT_POINTS * Pango.SCALE)
            self._layouts.clear()
        last = self.grid.rows[-1] + 1 if self.grid is not None and self.grid.rows else 1
        _layout, w, h, _numeric = self._text(str(max(last, 999)))
        self._hw, self._hh = max(36, w + 4 * PAD), h + 2 * PAD

    def _text(self, text: str, key: Any = None) -> tuple[Pango.Layout, int, int, bool]:
        """A laid-out text with its pixel size and alignment, cached by cell position or by the text."""
        key = text if key is None else key
        got = self._layouts.get(key)
        if got is None:
            if len(self._layouts) > LAYOUT_CACHE:  # one out per one in: clearing all at once stalls a frame
                self._layouts.popitem(last=False)
            layout = self.create_pango_layout(text)
            layout.set_font_description(self._font)
            got = self._layouts[key] = (layout, *layout.get_pixel_size(), looks_numeric(text))
        return got

    def do_measure(self, _orientation, _for_size):
        return 0, 0, -1, -1

    def do_size_allocate(self, _width, _height, _baseline):
        self._measure_headers()
        self._configure()

    # Drawing

    def _ink(self) -> Gdk.RGBA:
        return self.get_color() if _HAS_GET_COLOR else self.get_style_context().get_color()

    def do_snapshot(self, snap):
        width, height = self.get_width(), self.get_height()
        fg = self._ink()
        pal = _PALETTES[_luma(fg) > 0.5]  # light text means a dark theme
        snap.append_color(pal["bg"], _rect(0, 0, width, height))
        if self.sel is None:
            return
        hw, hh, xs, ys = self._hw, self._hh, self._xs, self._ys
        ox, oy = self._offset()
        i0, i1 = _span(ys, oy, oy + height - hh)
        j0, j1 = _span(xs, ox, ox + width - hw)
        dx, dy = hw - ox, hh - oy
        snap.push_clip(_rect(hw, hh, width - hw, height - hh))
        self._draw_cells(snap, pal, fg, i0, i1, j0, j1, dx, dy)
        snap.pop()

        sel = self._box(*self.sel)
        snap.append_color(pal["head"], _rect(0, 0, width, hh))
        snap.append_color(pal["head"], _rect(0, hh, hw, height - hh))
        for j in range(j0, j1 + 1):
            x, w = xs[j] + dx, xs[j + 1] - xs[j]
            clip = _rect(max(x, hw), 0, x + w - max(x, hw), hh)
            self._draw_header(snap, pal, fg, self.column_label(j), clip, x, w, sel[1] <= j <= sel[3], True)
        for i in range(i0, i1 + 1):
            y, h = ys[i] + dy, ys[i + 1] - ys[i]
            clip = _rect(0, max(y, hh), hw, y + h - max(y, hh))
            self._draw_header(snap, pal, fg, self.row_label(i), clip, y, h, sel[0] <= i <= sel[2], False)
        snap.append_color(pal["edge"], _rect(0, hh - 1, width, 1))
        snap.append_color(pal["edge"], _rect(hw - 1, 0, 1, height))

    def _draw_header(self, snap, pal, fg, label, clip, start, size, marked, vertical) -> None:
        snap.push_clip(clip)
        if marked:
            snap.append_color(pal["mark"], clip)
        layout, tw, th, _numeric = self._text(label)
        if vertical:
            _draw_text(snap, layout, start + (size - tw) / 2, (self._hh - th) / 2, fg)
            snap.append_color(pal["line"], _rect(start + size - 1, 0, 1, self._hh))
        else:
            _draw_text(snap, layout, (self._hw - tw) / 2, start + (size - th) / 2, fg)
            snap.append_color(pal["line"], _rect(0, start + size - 1, self._hw, 1))
        snap.pop()

    def _draw_cells(self, snap, pal, fg, i0, i1, j0, j1, dx, dy) -> None:
        xs, ys, rows, cols, get = self._xs, self._ys, self.grid.rows, self.grid.cols, self.grid.cells.get
        right, bottom = xs[j1 + 1] + dx, ys[i1 + 1] + dy
        for j in range(j0, j1 + 1):  # grid lines first: a fill hides its own, as in Calc and Excel
            snap.append_color(pal["line"], _rect(xs[j + 1] + dx - 1, self._hh, 1, bottom - self._hh))
        for i in range(i0, i1 + 1):
            snap.append_color(pal["line"], _rect(self._hw, ys[i + 1] + dy - 1, right - self._hw, 1))

        merges = dict.fromkeys(self._merges_in(i0, i1, j0, j1))
        texts = []
        for i in range(i0, i1 + 1):
            row, y, h = rows[i], ys[i] + dy, ys[i + 1] - ys[i]
            for j in range(j0, j1 + 1):
                cell = get((row, cols[j]))
                if cell is None:
                    continue
                if cell.rowspan > 1 or cell.colspan > 1:  # also found here before the index is ready
                    merges.setdefault(self._box(i, j))
                    continue
                if cell.fill and (color := _fill(cell.fill)[0]) is not None:
                    snap.append_color(color, _rect(xs[j] + dx, y, xs[j + 1] - xs[j], h))
                if cell.text:
                    texts.append((i, j, i, j, cell))
        covered = set()
        for m0, n0, m1, n1 in merges:
            cell = get((rows[m0], cols[n0]))
            x, y, w, h = xs[n0] + dx, ys[m0] + dy, xs[n1 + 1] - xs[n0], ys[m1 + 1] - ys[m0]
            color = _fill(cell.fill)[0] if cell is not None and cell.fill else None
            if color is not None:
                snap.append_color(color, _rect(x, y, w, h))
            else:  # cover the lines inside the merge, keep its outline
                snap.append_color(pal["bg"], _rect(x, y, w - 1, h - 1))
            if cell is not None and cell.text:
                texts.append((m0, n0, m1, n1, cell))
            covered.update((a, b) for a in range(max(m0, i0), min(m1, i1) + 1)
                           for b in range(max(n0, j0), min(n1, j1) + 1))

        for a0, b0, a1, b1, cell in texts:
            layout, tw, th, numeric = self._text(cell.text, (a0, b0))
            x, y, w, h = xs[b0] + dx, ys[a0] + dy, xs[b1 + 1] - xs[b0], ys[a1 + 1] - ys[a0]
            reach, k = w, b1
            while (not numeric and tw + 2 * PAD > reach and k < j1 and (a0, k + 1) not in covered
                   and get((rows[a0], cols[k + 1])) is None):  # long text runs on over empty cells
                k += 1
                reach += xs[k + 1] - xs[k]
            ink = _INK[_fill(cell.fill)[1]] if cell.fill and _fill(cell.fill)[0] is not None else fg
            # bottom-aligned as spreadsheets draw it: a line taller than its row (a 19 px UI font line in
            # a 17 px row) loses empty ascent at the top, not its descenders
            tx, ty = x + w - PAD - tw if numeric else x + PAD, y + h - th - 1
            if tw + 2 * PAD <= w and th < h:  # most cells: the text fits, no clip needed
                _draw_text(snap, layout, tx, ty, ink)
                continue
            snap.push_clip(_rect(x, y, reach - 1, h - 1))
            _draw_text(snap, layout, tx, ty, ink)
            snap.pop()

        hits = [(i, j) for i in range(i0, i1 + 1) for j in range(j0, j1 + 1) if (i, j) in self.hits]
        hits += [m[:2] for m in merges if m[:2] in self.hits and m[:2] not in hits]
        for i, j in hits:
            color = _CURRENT if (i, j) == self.current_hit else _MATCH
            snap.append_color(color, _rect(*self.cell_rect(i, j)))
        x, y, w, h = self.cell_rect(*self.sel)
        outline = Gsk.RoundedRect()  # kept in a name: PyGObject does not copy this plain struct
        outline.init_from_rect(_rect(x - 1, y - 1, w + 1, h + 1), 0)
        snap.append_border(outline, [2] * 4, [pal["sel"]] * 4)

    # Selection and keys

    def select(self, i: int, j: int) -> None:
        """Select a cell (a covered one selects its merge) and scroll it into view."""
        if self.sel is None:
            return
        i, j = self._owner(min(max(0, i), len(self._ys) - 2), min(max(0, j), len(self._xs) - 2))
        self.sel = (i, j)
        self.scroll_to(i, j)
        self.queue_draw()
        callback = self._on_select()
        if callback is not None:
            callback()

    def scroll_to(self, i: int, j: int) -> None:
        i0, j0, i1, j1 = self._box(i, j)
        for adj, edges, a, b in ((self._vadj, self._ys, i0, i1), (self._hadj, self._xs, j0, j1)):
            if adj is None:
                continue
            start, end, page, value = edges[a], edges[b + 1], adj.get_page_size(), adj.get_value()
            if page <= 0 or start < value:
                value = start
            elif end > value + page:
                value = min(start, end - page)
            adj.set_value(value)

    def _on_click(self, _gesture, _n, x, y):
        self.grab_focus()
        cell = self.at(x, y)
        if cell is not None:
            self.select(*cell)

    def _on_key_pressed(self, _controller, keyval, _code, state) -> bool:
        return self.handle_key(keyval, state)

    def handle_key(self, keyval: int, state: Gdk.ModifierType = 0) -> bool:
        """Arrows, Page Up/Down, Home/End and Ctrl+Home/End move the selection; True when used."""
        if self.sel is None:
            return False
        i, j = self.sel
        i0, j0, i1, j1 = self._box(i, j)
        last_i, last_j = len(self._ys) - 2, len(self._xs) - 2
        ctrl = bool(state & Gdk.ModifierType.CONTROL_MASK)
        page = self._vadj.get_page_size() if self._vadj else self.get_height()
        steps = {
            Gdk.KEY_Right: (i, j1 + 1), Gdk.KEY_Left: (i, j0 - 1), Gdk.KEY_Down: (i1 + 1, j),
            Gdk.KEY_Up: (i0 - 1, j), Gdk.KEY_Home: (0, 0) if ctrl else (i, 0),
            Gdk.KEY_End: (last_i, last_j) if ctrl else (i, last_j),
            Gdk.KEY_Page_Down: (bisect.bisect_right(self._ys, self._ys[i] + page) - 1, j),
            Gdk.KEY_Page_Up: (bisect.bisect_right(self._ys, self._ys[i] - page) - 1, j),
        }
        keyval = _KEYPAD.get(keyval, keyval)
        target = steps.get(keyval)
        if target is None:
            return False
        if keyval in (Gdk.KEY_Page_Down, Gdk.KEY_Page_Up) and self._vadj:  # the cell keeps its screen place
            self._vadj.set_value(self._vadj.get_value() + (page if keyval == Gdk.KEY_Page_Down else -page))
        self.select(*target)
        return True

    def do_dispose(self):
        if self._index_id:
            GLib.source_remove(self._index_id)
            self._index_id = 0
        Gtk.Widget.do_dispose(self)

