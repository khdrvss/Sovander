"""A closed document must leave memory: views connected from C with strong handlers never do."""

import gc
import threading
import time
import weakref

import pytest
from gi.repository import GLib, Gtk

from sovander.views.page_view import PageView
from sovander.views.present import PresentWindow

pytestmark = pytest.mark.gui


def _spin(seconds):
    context = GLib.MainContext.default()
    end = time.monotonic() + seconds
    while time.monotonic() < end:
        context.iteration(False)


def _freed(ref) -> bool:
    for _ in range(3):
        _spin(0.1)
        gc.collect()
    return ref() is None


def _open_page_view(pdf):
    window, scroller, view = Gtk.Window(), Gtk.ScrolledWindow(), PageView()
    window.set_child(scroller)
    scroller.set_child(view)
    view.load(pdf)
    window.present()
    _spin(0.3)
    return window, scroller, view


def test_closed_page_view_is_freed(make_pdf):
    window, scroller, view = _open_page_view(make_pdf(5))
    gone = weakref.ref(view)
    del view
    scroller.set_child(None)
    window.destroy()
    del window, scroller
    assert _freed(gone)


def test_closed_page_views_stop_their_render_threads(make_pdf):
    def render_threads():
        return sum(t.name == "sovander-render" and t.is_alive() for t in threading.enumerate())

    before = render_threads()
    for _ in range(3):
        window, scroller, view = _open_page_view(make_pdf(3))
        del view
        scroller.set_child(None)
        window.destroy()
        del window, scroller
        _freed(lambda: None)
    for _ in range(20):
        if render_threads() <= before:
            break
        _spin(0.05)
    assert render_threads() <= before


def test_destroyed_presentation_lets_its_page_view_go(make_pdf):
    window, scroller, view = _open_page_view(make_pdf(5))
    slides = PresentWindow(view, 0)
    slides.present()
    _spin(0.3)
    gone = weakref.ref(view)
    slides.destroy()  # destroyed without a close request, as a test or a crash would
    del slides, view
    scroller.set_child(None)
    window.destroy()
    del window, scroller
    assert _freed(gone)
