"""Presentation mode: full screen, one slide at a time, Escape returns the slide (Requirements 5.4, 5.5)."""

import gi
import pytest

from sovander.views.present import PresentWindow, present

gi.require_version("Gdk", "4.0")
from gi.repository import Gdk  # noqa: E402

try:
    import pymupdf as fitz
except ImportError:
    import fitz

pytestmark = pytest.mark.gui

SLIDE = (960, 540)
RED_BOX_CENTRE = (172, 200)  # drawn on every page by conftest.build_pdf


@pytest.fixture
def deck(viewer, make_pdf):
    viewer.load(make_pdf(5, size=SLIDE))
    viewer.mode = "slides"
    return viewer


@pytest.fixture
def show():
    windows = []

    def open_window(view, page=None):
        window = PresentWindow(view, page)
        windows.append(window)
        return window

    yield open_window
    for window in windows:
        window.destroy()


def test_starts_on_the_current_slide_and_steps_with_keys(deck, show):
    deck.go_to(2)
    window = show(deck)
    assert window.page == 2
    steps = [(Gdk.KEY_Right, 3), (Gdk.KEY_Down, 4), (Gdk.KEY_Page_Down, 4), (Gdk.KEY_space, 4)]
    steps += [
        (Gdk.KEY_Left, 3),
        (Gdk.KEY_Up, 2),
        (Gdk.KEY_Page_Up, 1),
        (Gdk.KEY_Home, 0),
        (Gdk.KEY_Page_Up, 0),
    ]
    steps += [(Gdk.KEY_End, 4)]
    for key, expected in steps:
        assert window.handle_key(key), Gdk.keyval_name(key)
        assert window.page == expected, Gdk.keyval_name(key)
    assert window.handle_key(Gdk.KEY_space, Gdk.ModifierType.SHIFT_MASK) and window.page == 3
    assert not window.handle_key(Gdk.KEY_a)


def test_clicks_move_forward_and_right_click_goes_back(deck, show):
    window = show(deck, 0)
    window.on_click(Gdk.BUTTON_PRIMARY)
    assert window.page == 1
    window.on_click(Gdk.BUTTON_SECONDARY)
    assert window.page == 0


def test_escape_leaves_on_the_current_slide(deck, wait):
    window = present(deck, page=1)
    finished = []
    window.connect("finished", lambda _window, page: finished.append(page))
    window.handle_key(Gdk.KEY_Right)
    window.handle_key(Gdk.KEY_Right)
    assert window.handle_key(Gdk.KEY_Escape)
    assert finished == [3]
    assert deck.current_page == 3  # the page view is back on the slide that was showing
    assert wait(lambda: not window.get_visible())


def test_slide_is_scaled_to_fit_on_black(deck, show, wait, pixels):
    window = show(deck, 0)
    window.set_default_size(1000, 500)
    window.present()
    slide = window.get_child()
    assert wait(lambda: slide.get_width() > 0 and window.is_ready())
    w, h = slide.get_width(), slide.get_height()
    k = min(w / SLIDE[0], h / SLIDE[1])
    left, top = round((w - SLIDE[0] * k) / 2), round((h - SLIDE[1] * k) / 2)
    image = pixels(slide)
    assert image.pixel(1, 1)[:3] == (0, 0, 0)  # the window is not 16:9, so a corner is a black bar
    red = image.pixel(int(left + RED_BOX_CENTRE[0] * k), int(top + RED_BOX_CENTRE[1] * k))[:3]
    assert all(abs(a - b) <= 3 for a, b in zip(red, (204, 25, 25), strict=True))
    assert image.pixel(int(left + 800 * k), int(top + 400 * k))[:3] == (255, 255, 255)


def test_slides_that_arrive_later_can_be_shown(viewer, make_pdf, show):
    full = make_pdf(5, size=SLIDE)
    first = fitz.open(stream=full, filetype="pdf")
    first.select([0])
    viewer.load(first.tobytes())
    viewer.set_loading_more(True)
    window = show(viewer, 0)
    window.next()
    assert window.page == 0
    viewer.replace(full)
    viewer.set_loading_more(False)
    window.next()
    assert window.page == 1
