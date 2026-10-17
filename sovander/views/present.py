"""Presentation mode: one slide at a time, full screen on black (Requirements 5.4, 5.5).

Slides are drawn from the page view's own document, so slides that arrive later (5.7) can be shown too.
"""

from functools import partial

import gi

from sovander.i18n import _
from sovander.views import weak_handler
from sovander.views.page_view import NEXT_KEYS, PREV_KEYS
from sovander.views.render import RenderWorker, device_scale, render_texture

gi.require_version("Gdk", "4.0")
gi.require_version("Graphene", "1.0")
gi.require_version("Gtk", "4.0")
from gi.repository import Gdk, GObject, Graphene, Gtk  # noqa: E402

_BLACK = Gdk.RGBA()
_BLACK.parse("black")


class PresentWindow(Gtk.Window):
    """Full-screen slides of a PageView; emits ``finished(page)`` when it closes, by Escape or otherwise."""

    __gtype_name__ = "SovanderPresentWindow"
    __gsignals__ = {"finished": (GObject.SignalFlags.RUN_FIRST, None, (int,))}

    def __init__(self, view, page=None, **props):
        super().__init__(title=_("Presentation"), default_width=960, default_height=540, **props)
        self._view, self._done = view, False
        self._slide = _Slide(view, view.current_page if page is None else page)
        self.set_child(self._slide)
        keys = Gtk.EventControllerKey()
        keys.connect("key-pressed", weak_handler(self._on_key))
        self.add_controller(keys)
        click = Gtk.GestureClick(button=0)
        click.connect("released", weak_handler(self._on_released))
        self._slide.add_controller(click)
        self.connect("close-request", weak_handler(self._on_close_request))
        self.fullscreen()

    def _on_key(self, _controller, keyval, _code, state):
        return self.handle_key(keyval, state)

    def _on_released(self, gesture, *_args):
        self.on_click(gesture.get_current_button())

    @property
    def page(self) -> int:
        """The slide shown, 0-based."""
        return self._slide.page

    def go_to(self, page):
        self._slide.show(page)

    def next(self):
        self.go_to(self.page + 1)

    def previous(self):
        self.go_to(self.page - 1)

    def is_ready(self) -> bool:
        """True once the current slide is drawn at the window's size."""
        return self._slide.is_ready()

    def handle_key(self, keyval, state=0) -> bool:
        """Slide keys (Requirements 5.2, 5.3) and Escape to leave (5.5); True when the key was used."""
        if keyval == Gdk.KEY_Escape:
            self.close()
        elif keyval in PREV_KEYS or (keyval == Gdk.KEY_space and state & Gdk.ModifierType.SHIFT_MASK):
            self.previous()
        elif keyval in NEXT_KEYS:
            self.next()
        elif keyval in (Gdk.KEY_Home, Gdk.KEY_End):
            self.go_to(0 if keyval == Gdk.KEY_Home else self._view.page_count - 1)
        else:
            return False
        return True

    def on_click(self, button):
        """A click moves to the next slide; the right button goes back."""
        if button == Gdk.BUTTON_SECONDARY:
            self.previous()
        else:
            self.next()

    def _on_close_request(self, _window):
        if not self._done:
            self._done = True
            self._slide.stop()
            self.emit("finished", self.page)
        return False


def present(view, page=None, application=None) -> PresentWindow:
    """Start presenting ``view``; when it ends, the view shows the slide that was on screen (5.5)."""
    window = PresentWindow(view, page, application=application)
    window.connect("finished", lambda _window, last: view.go_to(last))
    window.present()
    return window


class _Slide(Gtk.Widget):
    """Black area with the current slide scaled to fit; the neighbours are drawn ahead."""

    __gtype_name__ = "SovanderPresentSlide"

    def __init__(self, view, page):
        super().__init__(hexpand=True, vexpand=True)
        self._view, self._page, self._textures = view, 0, {}
        self._worker = RenderWorker(weak_handler(self._deliver))
        self._count_id = view.connect("notify::page-count", weak_handler(self._on_page_count))
        self.set_cursor_from_name("none")
        self.show(page)

    @property
    def page(self) -> int:
        return self._page

    def _on_page_count(self, *_args):
        self.show(self._page)

    def show(self, page):
        self._page = min(max(0, int(page)), max(0, self._view.page_count - 1))
        self._request()
        self.queue_draw()

    def stop(self):
        self._worker.close()
        if self._count_id:
            self._view.disconnect(self._count_id)
            self._count_id = 0

    def do_unroot(self):
        # A destroyed window never asks to close, and queued jobs would keep this slide and its view alive
        self.stop()
        Gtk.Widget.do_unroot(self)

    def is_ready(self) -> bool:
        have = self._textures.get(self._page)
        return have is not None and have[0] == self._key(self._page)

    def _fit(self, page):
        """Logical pixels per point that fit ``page`` on screen."""
        w, h = self._view.page_size(page)
        return min(self.get_width() / w, self.get_height() / h)

    def _key(self, page):
        return page, round(self._fit(page) * device_scale(self), 4), id(self._view.document)

    def _request(self):
        doc, count = self._view.document, self._view.page_count
        if doc is None or self.get_width() <= 0 or self.get_height() <= 0:
            return
        wanted = [p for p in (self._page, self._page + 1, self._page - 1) if 0 <= p < count]
        self._textures = {p: t for p, t in self._textures.items() if p in wanted}
        alive = lambda: doc is self._view.document  # noqa: E731 - a replaced document makes jobs stale
        jobs = []
        for page in wanted:
            key = self._key(page)
            if self._textures.get(page, (None,))[0] != key:
                jobs.append((key, partial(render_texture, doc, page, key[1], alive=alive)))
        self._worker.submit(jobs)

    def _deliver(self, key, result):
        page = key[0]
        if result is None or isinstance(result, Exception) or abs(page - self._page) > 1:
            return
        self._textures[page] = (key, result[0])  # an older size stays until this replaces it
        if page == self._page:
            self.queue_draw()

    def do_size_allocate(self, _width, _height, _baseline):
        self._request()

    def do_snapshot(self, snap):
        w, h = self.get_width(), self.get_height()
        snap.append_color(_BLACK, Graphene.Rect().init(0, 0, w, h))
        have = self._textures.get(self._page)
        if have is None or not self._view.page_count:
            return
        k = self._fit(self._page)
        sw, sh = (side * k for side in self._view.page_size(self._page))
        snap.append_texture(have[1], Graphene.Rect().init(round((w - sw) / 2), round((h - sh) / 2), sw, sh))

    def do_dispose(self):
        self.stop()
        Gtk.Widget.do_dispose(self)
