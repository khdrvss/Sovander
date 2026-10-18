"""Page view: a PDF shown top to bottom, drawn by MuPDF (Requirements 2.1, 3.1-3.4, 5.1-5.3, 5.7).

PageView implements Gtk.Scrollable, like Gtk.TextView: put it in a Gtk.ScrolledWindow. Pages are drawn in
tiles off the main thread (render.py), only near the viewport; a page not drawn yet is a blank box of its
real size, so the scrollbar is right from the first frame. Page indexes are 0-based everywhere.
"""

import bisect
import weakref
from itertools import accumulate

import gi

from sovander import i18n
from sovander.i18n import _
from sovander.views import to_main_loop, weak_handler
from sovander.views.render import FITZ_LOCK, PasswordRequired, TileRenderer, device_scale, open_pdf

try:
    import pymupdf as fitz
except ImportError:  # PyMuPDF before 1.24 (Ubuntu 22.04 and 24.04) has only the old name
    import fitz

gi.require_version("Gdk", "4.0")
gi.require_version("Graphene", "1.0")
gi.require_version("Gsk", "4.0")
gi.require_version("Gtk", "4.0")
from gi.repository import Gdk, GLib, GObject, Graphene, Gsk, Gtk  # noqa: E402

__all__ = ["FITZ_LOCK", "NEXT_KEYS", "PREV_KEYS", "PageView", "PasswordRequired"]

PX_PER_PT = 96 / 72  # zoom 1.0 shows the page at its printed size on a 96 dpi screen
GAP = MARGIN = 12  # equal, so a page moved to the top shows no sliver of the one before
LOADING_ROW = 56
ZOOM_MIN, ZOOM_MAX = 0.1, 8.0
ZOOM_STEPS = (0.25, 0.33, 0.5, 0.67, 0.8, 1.0, 1.25, 1.5, 2.0, 3.0, 4.0, 6.0, 8.0)
KEEP_PAGES = 3  # tiles of pages further than this from the viewport are dropped

NEXT_KEYS = {Gdk.KEY_Right, Gdk.KEY_Down, Gdk.KEY_Page_Down, Gdk.KEY_KP_Page_Down, Gdk.KEY_space}
PREV_KEYS = {Gdk.KEY_Left, Gdk.KEY_Up, Gdk.KEY_Page_Up, Gdk.KEY_KP_Page_Up, Gdk.KEY_BackSpace}
ZOOM_KEYS = {Gdk.KEY_plus: 1, Gdk.KEY_equal: 1, Gdk.KEY_KP_Add: 1, Gdk.KEY_minus: -1, Gdk.KEY_KP_Subtract: -1}
ZOOM_KEYS |= {Gdk.KEY_0: 0, Gdk.KEY_KP_0: 0}


def _rgba(spec):
    color = Gdk.RGBA()
    color.parse(spec)
    return color


_PAPER, _NIGHT_PAPER, _EDGE = _rgba("white"), _rgba("black"), _rgba("rgba(0,0,0,0.25)")


def _readonly(kind, attr, blurb=""):
    getter = lambda self: getattr(self, attr)  # noqa: E731
    flags = GObject.ParamFlags.READABLE
    return GObject.Property(type=kind, default=kind(), getter=getter, flags=flags, blurb=blurb)


class PageView(Gtk.Widget, Gtk.Scrollable):
    """All pages of one PDF in a column. Put it in a Gtk.ScrolledWindow."""

    __gtype_name__ = "SovanderPageView"

    current_page = _readonly(int, "_current", "The page filling most of the view (Requirements 3.1, 5.1)")
    page_count = _readonly(int, "_count")
    zoom = _readonly(float, "_zoom", "1.0 is the printed size")
    sizing = _readonly(str, "_sizing", '"free", "fit-width" or "fit-page"; a fit is kept on resize')
    loading_more = _readonly(bool, "_loading", "A row under the last page says more pages are coming")
    hscroll_policy = GObject.Property(type=Gtk.ScrollablePolicy, default=Gtk.ScrollablePolicy.MINIMUM)
    vscroll_policy = GObject.Property(type=Gtk.ScrollablePolicy, default=Gtk.ScrollablePolicy.MINIMUM)
    lock = FITZ_LOCK

    def __init__(self):
        self._hadj = self._vadj = None
        self._adj_ids = {}
        super().__init__(focusable=True, overflow=Gtk.Overflow.HIDDEN)
        self._count, self._sizes, self._pw, self._ph, self._tops = 0, [], [], [], []
        self._content, self._zoom, self._sizing, self._mode = (0, 0), 1.0, "free", "continuous"
        self._loading = self._allocating = False
        self._current, self._pinned, self._goto_pending, self._pointer = 0, None, None, None
        self._pinch, self._update_id = (1.0, None), 0
        # The render thread outlives any one view, so it must not hold this one
        self._render = TileRenderer(weak_handler(self._on_tile))
        # PyGObject never runs a Python do_dispose once the wrapper is gone, and a finalizer runs after GTK
        # has freed the widget: so this view has no child widgets, and the finalizer only stops the thread
        weakref.finalize(self, self._render.close)
        i18n.add_listener(self._relabel)
        self._add_controllers()
        self.connect("notify::scale-factor", weak_handler(self._on_scale_factor))

    def _add_controllers(self):
        keys = Gtk.EventControllerKey()
        keys.connect("key-pressed", weak_handler(self._on_key))
        wheel = Gtk.EventControllerScroll.new(Gtk.EventControllerScrollFlags.VERTICAL)
        wheel.connect("scroll", weak_handler(self._on_wheel))
        motion = Gtk.EventControllerMotion()
        motion.connect("motion", weak_handler(self._on_motion))
        motion.connect("leave", weak_handler(self._on_leave))
        pinch = Gtk.GestureZoom()
        pinch.connect("begin", weak_handler(self._on_pinch_begin))
        pinch.connect("scale-changed", weak_handler(self._on_pinch_scale))
        click = Gtk.GestureClick()
        click.connect("pressed", weak_handler(self._on_click))
        for controller in (keys, wheel, motion, pinch, click):
            self.add_controller(controller)

    def _on_scale_factor(self, *_args):
        self._queue_update()

    def _on_key(self, _controller, keyval, _code, state):
        return self.handle_key(keyval, state)

    def _on_motion(self, _controller, x, y):
        self._pointer = (x, y)

    def _on_leave(self, _controller):
        self._pointer = None

    def _on_pinch_scale(self, _gesture, scale):
        self.pinch_zoom(scale)

    def _on_click(self, *_args):
        self.grab_focus()

    def _relabel(self, _language):
        if self._loading:
            self.queue_draw()

    @GObject.Property(type=Gtk.Adjustment)
    def hadjustment(self):
        return self._hadj

    @hadjustment.setter
    def hadjustment(self, adj):
        self._hadj = self._adopt(self._hadj, adj)

    @GObject.Property(type=Gtk.Adjustment)
    def vadjustment(self):
        return self._vadj

    @vadjustment.setter
    def vadjustment(self, adj):
        self._vadj = self._adopt(self._vadj, adj)

    def _adopt(self, old, adj):
        if old is not None and old in self._adj_ids:
            old.disconnect(self._adj_ids.pop(old))
        if adj is not None:
            self._adj_ids[adj] = adj.connect("value-changed", weak_handler(self._on_scrolled))
        return adj

    @GObject.Property(type=str, default="continuous")
    def mode(self):
        """ "continuous" scrolls with the keys; "slides" moves one page per key (Requirements 5.2, 5.3)."""
        return self._mode

    @mode.setter
    def mode(self, mode):
        if mode not in ("continuous", "slides"):
            raise ValueError(mode)
        self._mode = mode

    @GObject.Property(type=bool, default=False)
    def night(self):
        """Light text on dark pages, photos kept (Requirement 12.2)."""
        return self._render.night

    @night.setter
    def night(self, on):
        if on != self._render.night:
            self._render.set_night(on)
            self._redraw()

    @property
    def document(self):
        """The fitz.Document shown, for search, selection and links; call into it under ``lock``."""
        return self._render.doc

    @property
    def texture_bytes(self) -> int:
        return self._render.nbytes

    def page_size(self, page) -> tuple[float, float]:
        """Page size in points."""
        return self._sizes[page]

    def load(self, pdf, password=None):
        """Show a PDF (bytes or path) from its first page; raises PasswordRequired."""
        self._swap_document(open_pdf(pdf, password), keep_position=False)

    def replace(self, pdf, password=None):
        """Swap in a fuller copy of the shown document (all pages after page 1) without moving."""
        self._swap_document(open_pdf(pdf, password), keep_position=True)

    def set_loading_more(self, on: bool):
        """Show or hide the row under the last page saying more pages are coming (Requirement 5.7)."""
        if on != self._loading:
            self._loading = on
            self._relayout()
            self.queue_allocate()
            self.notify("loading-more")

    def _swap_document(self, doc, keep_position):
        spot = self._top_spot() if keep_position else None
        with FITZ_LOCK:
            sizes = [(page.rect.width, page.rect.height) for page in doc]  # 756 pages: about 0.06 s
        self._render.set_document(doc, keep_below=len(sizes) if keep_position else 0)
        old_count, self._sizes, self._count, self._pinned = self._count, sizes, len(sizes), None
        self._relayout()
        if spot is not None and spot[0] < len(sizes):
            self._scroll_to_spot(spot, self.get_width() / 2, 0)
        else:
            for adj in (self._hadj, self._vadj):
                if adj is not None:
                    adj.set_value(0)
        if self._count != old_count:
            self.notify("page-count")
        self._update_current()
        self._redraw()

    def go_to(self, page):
        """Scroll so that ``page`` starts at the top of the view (Requirement 3.2)."""
        if not self._sizes:
            return
        page = min(max(0, int(page)), len(self._sizes) - 1)
        if self._vadj is None or self.get_height() <= 0:
            self._goto_pending = page
            self._set_current(page)
            return
        target = max(0, min(self._tops[page] - MARGIN, self._content[1] - self.get_height()))
        self._pinned = (page, target)  # the last pages cannot reach the top but still count as current
        self._vadj.set_value(target)
        self._update_current()

    def zoom_in(self):
        steps = [s for s in ZOOM_STEPS if s > self._zoom * 1.01]
        self.set_zoom(steps[0] if steps else ZOOM_MAX)

    def zoom_out(self):
        steps = [s for s in ZOOM_STEPS if s < self._zoom / 1.01]
        self.set_zoom(steps[-1] if steps else ZOOM_MIN)

    def zoom_reset(self):
        self.set_zoom(1.0)

    def set_zoom(self, zoom, anchor=None):
        """Zoom keeping the point under ``anchor`` (widget coordinates; default pointer or centre) still."""
        self._apply_zoom(zoom, anchor or self._pointer, "free")

    def fit_width(self):
        self._sizing = "fit-width"
        self._fit()

    def fit_page(self):
        self._sizing = "fit-page"
        self._fit()
        self.go_to(self._current)

    def pinch_zoom(self, scale):
        """Touchpad or touchscreen pinch; ``scale`` is relative to the zoom when the pinch began."""
        self._apply_zoom(self._pinch[0] * scale, self._pinch[1], "free")

    def _on_pinch_begin(self, gesture, _sequence):
        ok, x, y = gesture.get_bounding_box_center()
        self._pinch = (self._zoom, (x, y) if ok else None)

    def _on_wheel(self, controller, _dx, dy):
        if not controller.get_current_event_state() & Gdk.ModifierType.CONTROL_MASK:
            return False  # plain scrolling belongs to the scrolled window
        self._apply_zoom(self._zoom * 1.1**-dy, self._pointer, "free")
        return True

    def _fit(self):
        width, height = self.get_width() - 2 * MARGIN, self.get_height() - 2 * MARGIN
        if self._sizing == "free" or not self._sizes or width <= 0 or height <= 0:
            return
        if self._sizing == "fit-width":
            zoom = width / max(w for w, _h in self._sizes)
        else:
            w, h = self._sizes[self._current]
            zoom = min(width / w, height / h)
        self._apply_zoom(zoom / PX_PER_PT, (self.get_width() / 2, 0), self._sizing)

    def _apply_zoom(self, zoom, anchor, sizing):
        zoom = min(ZOOM_MAX, max(ZOOM_MIN, zoom))
        if sizing != self._sizing:
            self._sizing = sizing
            self.notify("sizing")
        if abs(zoom - self._zoom) < 1e-4:
            return
        ax, ay = anchor if anchor else (self.get_width() / 2, self.get_height() / 2)
        ox, oy = self._offset()
        spot = self._locate(ax + ox, ay + oy, clamp=True)
        self._zoom = zoom
        self._relayout()
        if spot is not None:
            self._scroll_to_spot(spot, ax, ay)
        self.notify("zoom")
        self._redraw()

    def handle_key(self, keyval, state=0) -> bool:
        """Keyboard zoom, scrolling and slide steps; True when the key was used."""
        if state & Gdk.ModifierType.CONTROL_MASK:
            step = ZOOM_KEYS.get(keyval)
            if step is not None:
                {1: self.zoom_in, -1: self.zoom_out, 0: self.zoom_reset}[step]()
            return step is not None
        if keyval == Gdk.KEY_space and state & Gdk.ModifierType.SHIFT_MASK:
            keyval = Gdk.KEY_Page_Up
        if self._mode == "slides":
            target = {Gdk.KEY_Home: 0, Gdk.KEY_End: len(self._sizes) - 1}.get(keyval)
            if target is None and keyval in NEXT_KEYS | PREV_KEYS:
                target = self._current + (1 if keyval in NEXT_KEYS else -1)
            if target is not None:
                self.go_to(target)
            return target is not None
        page, v, h = self.get_height() * 0.9, self._vadj, self._hadj
        moves = {Gdk.KEY_Down: (v, 40), Gdk.KEY_Up: (v, -40), Gdk.KEY_Right: (h, 40), Gdk.KEY_Left: (h, -40)}
        moves |= {Gdk.KEY_Page_Down: (v, page), Gdk.KEY_space: (v, page), Gdk.KEY_Page_Up: (v, -page)}
        moves |= {Gdk.KEY_Home: (v, -1e12), Gdk.KEY_End: (v, 1e12)}
        adj, delta = moves.get(keyval, (None, 0))
        if adj is not None:
            adj.set_value(adj.get_value() + delta)
        return adj is not None

    def page_rect_to_widget(self, page, rect) -> Graphene.Rect:
        """Where ``rect`` (fitz.Rect or x0, y0, x1, y1 in page points) is drawn, in widget coordinates."""
        x0, y0, x1, y1 = tuple(rect)
        x, y, w, h = self._page_box(page)
        sx, sy = w / self._sizes[page][0], h / self._sizes[page][1]
        ox, oy = self._offset()
        return Graphene.Rect().init(x - ox + x0 * sx, y - oy + y0 * sy, (x1 - x0) * sx, (y1 - y0) * sy)

    def widget_point_to_page(self, x, y):
        """(page, fitz.Point in page points) under a widget point, or None between pages."""
        ox, oy = self._offset()
        spot = self._locate(x + ox, y + oy, clamp=False)
        return None if spot is None else (spot[0], fitz.Point(spot[1], spot[2]))

    def _relayout(self):
        k = self._zoom * PX_PER_PT
        self._pw = [max(1, round(w * k)) for w, _h in self._sizes]
        self._ph = [max(1, round(h * k)) for _w, h in self._sizes]
        tops = accumulate(self._ph[:-1], lambda top, h: top + h + GAP, initial=MARGIN)
        self._tops = list(tops)[: len(self._ph)]
        bottom = self._tops[-1] + self._ph[-1] + MARGIN if self._ph else 0
        self._content = (max(self._pw, default=0) + 2 * MARGIN, bottom + LOADING_ROW * self._loading)
        self._configure()

    def _configure(self):
        w, h = self.get_width(), self.get_height()
        for adj, size, total in ((self._hadj, w, self._content[0]), (self._vadj, h, self._content[1])):
            if adj is not None:
                adj.configure(adj.get_value(), 0, max(total, size), 40, size * 0.9, size)

    def _offset(self):
        return (self._hadj.get_value() if self._hadj else 0.0, self._vadj.get_value() if self._vadj else 0.0)

    def _page_box(self, i):
        """Page ``i`` as (x, y, width, height) in content coordinates."""
        x = (max(self._content[0], self.get_width()) - self._pw[i]) // 2
        return x, self._tops[i], self._pw[i], self._ph[i]

    def _visible(self, top, bottom):
        if not self._tops:
            return range(0)
        first = max(0, bisect.bisect_right(self._tops, top) - 1)
        if first + 1 < len(self._tops) and self._tops[first] + self._ph[first] <= top:
            first += 1  # that page ends in the gap above the view
        last = min(len(self._tops) - 1, bisect.bisect_left(self._tops, bottom) - 1)
        return range(first, max(first, last) + 1)

    def _locate(self, cx, cy, clamp):
        """(page, x, y in points) at a content point; nearest page when ``clamp``, else None off pages."""
        if not self._tops:
            return None
        i = min(len(self._tops) - 1, max(0, bisect.bisect_right(self._tops, cy) - 1))
        x, y, w, h = self._page_box(i)
        if not clamp and not (x <= cx <= x + w and y <= cy <= y + h):
            return None
        fx, fy = min(max((cx - x) / w, 0.0), 1.0), min(max((cy - y) / h, 0.0), 1.0)
        return i, fx * self._sizes[i][0], fy * self._sizes[i][1]

    def _top_spot(self):
        ox, oy = self._offset()
        return self._locate(ox + self.get_width() / 2, oy, clamp=True)

    def _scroll_to_spot(self, spot, ax, ay):
        i, px, py = spot
        x, y, w, h = self._page_box(i)
        if self._hadj is not None:
            self._hadj.set_value(x + px * w / self._sizes[i][0] - ax)
        if self._vadj is not None:
            self._vadj.set_value(y + py * h / self._sizes[i][1] - ay)

    def _on_scrolled(self, _adj):
        if self._pinned and self._vadj and abs(self._vadj.get_value() - self._pinned[1]) > 0.5:
            self._pinned = None
        self._update_current()
        if self._loading and not self._allocating:
            self.queue_allocate()
        self._redraw()

    def _update_current(self):
        if self._pinned:
            self._set_current(self._pinned[0])
            return
        ox, oy = self._offset()
        bottom, best, current = oy + self.get_height(), -1.0, 0
        for i in self._visible(oy, bottom):
            seen = min(bottom, self._tops[i] + self._ph[i]) - max(oy, self._tops[i])
            if seen > best + 0.5:
                best, current = seen, i
        self._set_current(current)

    def _set_current(self, page):
        if page != self._current:
            self._current = page
            self.notify("current-page")

    def do_measure(self, _orientation, _for_size):
        return 0, 0, -1, -1

    def do_size_allocate(self, width, height, _baseline):
        self._allocating = True
        try:
            self._fit()
            self._configure()
            if self._goto_pending is not None:
                page, self._goto_pending = self._goto_pending, None
                self.go_to(page)
            self._update_current()
            self._queue_update()
        finally:
            self._allocating = False

    def do_snapshot(self, snap):
        ox, oy = self._offset()
        paper = _NIGHT_PAPER if self._render.night else _PAPER
        for i in self._visible(oy, oy + self.get_height()):
            x, y, pw, ph = self._page_box(i)
            x, y = x - ox, y - oy
            box = Graphene.Rect().init(x, y, pw, ph)
            snap.append_color(paper, box)
            sx, sy = pw / self._sizes[i][0], ph / self._sizes[i][1]
            for texture, (x0, y0, x1, y1) in self._render.tiles(i):
                where = Graphene.Rect().init(x + x0 * sx, y + y0 * sy, (x1 - x0) * sx, (y1 - y0) * sy)
                snap.append_texture(texture, where)
            outline = Gsk.RoundedRect()  # kept in a name: PyGObject does not copy this plain struct
            outline.init_from_rect(box, 0)
            snap.append_border(outline, [1] * 4, [_EDGE] * 4)
        if self._loading:
            self._draw_loading_row(snap, oy)

    def _draw_loading_row(self, snap, oy):
        layout = self.create_pango_layout(_("More pages are loading…"))
        width, height = layout.get_pixel_size()
        y = self._content[1] - oy - (LOADING_ROW + height) // 2
        snap.save()
        snap.translate(Graphene.Point().init((self.get_width() - width) // 2, y))
        # Gtk.Widget.get_color arrived in GTK 4.10; Ubuntu 22.04 has 4.6
        color = self.get_color() if hasattr(self, "get_color") else self.get_style_context().get_color()
        snap.append_layout(layout, color)
        snap.restore()

    def is_page_rendered(self, page) -> bool:
        """True when the visible part of ``page`` (off screen: any part) is drawn at the current zoom."""
        ox, oy = self._offset()
        keys = self._keys(page, ox, oy, ox + self.get_width(), oy + self.get_height())
        return all(map(self._render.has, keys)) if keys else self._render.has_current(page)

    def _keys(self, i, left, top, right, bottom):
        if not 0 <= i < len(self._sizes):
            return []
        x, y, w, h = self._page_box(i)
        fx0, fy0, fx1, fy1 = (left - x) / w, (top - y) / h, (right - x) / w, (bottom - y) / h
        return self._render.keys(i, self._sizes[i], fx0, fy0, fx1, fy1)

    def _on_tile(self, _page):
        self.queue_draw()

    def _redraw(self):
        self.queue_draw()
        self._queue_update()

    def _queue_update(self):
        if not self._update_id:
            self._update_id = to_main_loop(self._update)  # tiles are asked for here: not below a spinner

    def _update(self):
        """Ask for the tiles in view, then one screen ahead and one behind; forget far pages."""
        self._update_id = 0
        w, h = self.get_width(), self.get_height()
        if not self._sizes or w <= 0 or h <= 0:
            return GLib.SOURCE_REMOVE
        self._render.scale = round(self._zoom * PX_PER_PT * device_scale(self), 4)
        ox, oy = self._offset()
        wanted = [
            key
            for top, bottom in ((oy, oy + h), (oy + h, oy + 2 * h), (oy - h, oy))
            for i in self._visible(top, bottom)
            for key in self._keys(i, ox, top, ox + w, bottom)
        ]
        shown = self._visible(oy, oy + h)
        keep = range(max(0, shown.start - KEEP_PAGES), shown.stop + KEEP_PAGES)
        self._render.request(wanted, self._sizes, keep, self._current)
        return GLib.SOURCE_REMOVE

