"""Text selection and copy on the page view (task 4.2, Requirement 3.5)."""

import gc
import weakref

import gi
import pytest

from sovander.views.page_view import PageView
from sovander.views.selection import HighlightLayer, Selection, join_words, nearest_word

gi.require_version("Gdk", "4.0")
gi.require_version("Gtk", "4.0")
from gi.repository import Gdk, Gtk  # noqa: E402

try:
    import pymupdf as fitz
except ImportError:
    import fitz

pytestmark = pytest.mark.gui

CTRL, SHIFT = Gdk.ModifierType.CONTROL_MASK, Gdk.ModifierType.SHIFT_MASK
PAGE_TEXT = {0: ("Alpha Beta Gamma", "Delta Epsilon"), 1: ("Second Page Words", "Last Line")}


def text_pdf(pages=4) -> bytes:
    """Small pages (two fit on screen) with two lines of words each."""
    doc = fitz.open()
    for i in range(pages):
        page = doc.new_page(width=400, height=300)
        first, second = PAGE_TEXT.get(i, (f"Page {i + 1} words", "More text"))
        page.insert_text((40, 60), first, fontsize=14)
        page.insert_text((40, 90), second, fontsize=14)
    return doc.tobytes()


class FakeGesture:
    def __init__(self, state=0):
        self.state = state

    def get_current_event_state(self):
        return self.state


@pytest.fixture
def selecting(viewer, window, wait):
    viewer.load(text_pdf())
    scrolled = viewer.get_parent()
    window.set_child(None)
    layer = HighlightLayer(viewer)
    window.set_child(layer.wrap(scrolled))
    assert wait(lambda: viewer.get_width() > 0 and layer.get_width() > 0)
    return viewer, layer, Selection(viewer, layer)


def centre(view, selection, page, word):
    (w,) = [w for w in selection.words(page) if w[4] == word]
    r = view.page_rect_to_widget(page, w[:4])
    return r.get_x() + r.get_width() / 2, r.get_y() + r.get_height() / 2


def drag(selection, start, end):
    selection.begin_drag(*start)
    selection.update_drag(*end)
    selection.end_drag()


def clipboard_text(widget, wait):
    out = []
    widget.get_clipboard().read_text_async(None, lambda c, r: out.append(c.read_text_finish(r)))
    assert wait(lambda: out)
    return out[0]


def test_drag_selects_whole_words_in_reading_order(selecting):
    view, _layer, sel = selecting
    x, y = centre(view, sel, 0, "Beta")
    drag(sel, (x + 3, y), centre(view, sel, 0, "Epsilon"))  # starting inside a word takes all of it
    assert sel.text == "Beta Gamma\nDelta Epsilon"


def test_backward_drag_gives_the_same_text(selecting):
    view, _layer, sel = selecting
    drag(sel, centre(view, sel, 0, "Epsilon"), centre(view, sel, 0, "Beta"))
    assert sel.text == "Beta Gamma\nDelta Epsilon"


def test_drag_across_pages(selecting):
    view, _layer, sel = selecting
    drag(sel, centre(view, sel, 0, "Gamma"), centre(view, sel, 1, "Words"))
    assert sel.text == "Gamma\nDelta Epsilon\nSecond Page Words"
    assert sel.span == ((0, 2), (1, 2))


def test_drag_into_the_gap_between_pages_reaches_the_nearest_page(selecting):
    view, _layer, sel = selecting
    bottom = view.page_rect_to_widget(0, (0, 0, 400, 300))
    gap_y = bottom.get_y() + bottom.get_height() + 3
    assert view.widget_point_to_page(200, gap_y) is None
    drag(sel, centre(view, sel, 0, "Beta"), (200, gap_y))
    assert sel.text == "Beta Gamma\nDelta Epsilon"


def test_drag_beside_the_text_takes_the_whole_line(selecting):
    view, _layer, sel = selecting
    _x, y = centre(view, sel, 0, "Delta")
    right = view.page_rect_to_widget(0, (390, 0, 391, 1)).get_x()
    drag(sel, centre(view, sel, 0, "Alpha"), (right, y))
    assert sel.text == "Alpha Beta Gamma\nDelta Epsilon"


def test_a_small_movement_is_a_click_not_a_selection(selecting):
    view, _layer, sel = selecting
    x, y = centre(view, sel, 0, "Beta")
    drag(sel, (x, y), (x + 2, y + 1))
    assert sel.empty and sel.text == ""


def test_double_click_selects_a_word_and_triple_click_a_line(selecting):
    view, _layer, sel = selecting
    x, y = centre(view, sel, 0, "Beta")
    sel._on_pressed(None, 2, x, y)
    assert sel.text == "Beta"
    sel._on_pressed(None, 3, x, y)
    assert sel.text == "Alpha Beta Gamma"
    assert not sel.select_word_at(
        *view.page_rect_to_widget(0, (300, 250, 301, 251)).get_center().to_vec2().to_float()
    )


def test_ctrl_a_selects_the_page_and_ctrl_c_copies(selecting, wait):
    view, _layer, sel = selecting
    assert not sel.handle_key(Gdk.KEY_c, CTRL)  # nothing selected: the key is left to others
    assert sel.handle_key(Gdk.KEY_a, CTRL)
    assert sel.text == "Alpha Beta Gamma\nDelta Epsilon"
    assert sel.handle_key(Gdk.KEY_c, CTRL)
    assert clipboard_text(view, wait) == "Alpha Beta Gamma\nDelta Epsilon"
    assert not sel.handle_key(Gdk.KEY_a, 0)


def test_copy_returns_and_places_the_text(selecting, wait):
    view, _layer, sel = selecting
    drag(sel, centre(view, sel, 0, "Delta"), centre(view, sel, 1, "Second"))
    assert sel.copy() == "Delta Epsilon\nSecond"
    assert clipboard_text(view, wait) == "Delta Epsilon\nSecond"


def test_plain_click_clears_and_shift_click_extends(selecting):
    view, _layer, sel = selecting
    sel.select_word_at(*centre(view, sel, 0, "Alpha"))
    sel._on_released(FakeGesture(SHIFT), 1, *centre(view, sel, 0, "Delta"))
    assert sel.text == "Alpha Beta Gamma\nDelta"
    sel._on_released(FakeGesture(0), 1, 5, 5)
    assert sel.empty


def test_changed_is_emitted(selecting):
    view, _layer, sel = selecting
    seen = []
    sel.connect("changed", lambda s: seen.append(s.text))
    sel.select_page(0)
    sel.select_page(0)  # same selection: no signal
    sel.clear()
    assert seen == ["Alpha Beta Gamma\nDelta Epsilon", ""]


def test_selection_is_highlighted(selecting, wait, pixels):
    view, layer, sel = selecting
    sel.select_word_at(*centre(view, sel, 0, "Beta"))
    assert wait(lambda: layer.get_mapped())
    image = pixels(layer)
    bx, by = centre(view, sel, 0, "Beta")
    dx, dy = centre(view, sel, 0, "Delta")
    r, g, b, a = image.pixel(int(bx), int(by))
    assert a > 60 and b > r
    assert image.pixel(int(dx), int(dy))[3] == 0


def test_a_new_document_drops_the_selection(selecting, make_pdf):
    view, _layer, sel = selecting
    sel.select_page(0)
    seen = []
    sel.connect("changed", lambda _s: seen.append(1))
    view.load(make_pdf(3))
    assert sel.empty and seen == [1]
    assert sel.words(0)[0][4] == "Page"


def test_dragging_past_the_bottom_scrolls(selecting, wait):
    view, _layer, sel = selecting
    adj = view.props.vadjustment
    x, y = centre(view, sel, 0, "Alpha")
    sel.begin_drag(x, y)
    sel.update_drag(x, view.get_height() + 30)
    assert wait(lambda: adj.get_value() > 100, 3)
    assert sel.span[1][0] >= 1  # the selection follows into the pages scrolled in
    sel.end_drag()
    stopped = adj.get_value()
    wait(lambda: False, 0.2)
    assert adj.get_value() == stopped


def test_nearest_word_rules():
    words = [(10, 10, 30, 20, "a", 0, 0, 0), (40, 10, 60, 20, "b", 0, 0, 1), (10, 30, 30, 40, "c", 0, 1, 0)]
    assert nearest_word(words, 45, 15) == 1  # inside
    assert nearest_word(words, 200, 15) == 1  # right of the line: its last word
    assert nearest_word(words, 200, 5) == 0  # above all text
    assert nearest_word(words, 0, 100) == 2  # below all text
    assert nearest_word([], 0, 0) is None
    assert join_words(words) == "a b\nc"


def test_closed_view_is_freed_with_its_selection(wait):
    # Handlers kept from C must not hold the view, or every closed document would stay in memory
    window = Gtk.Window()
    view = PageView()
    layer = HighlightLayer(view)
    window.set_child(layer.wrap(Gtk.ScrolledWindow(child=view)))
    view.load(text_pdf())
    window.present()
    wait(lambda: False, 0.2)
    selection = Selection(view, layer)
    selection.select_page(0)
    gone = [weakref.ref(o) for o in (view, layer, selection)]
    del view, layer, selection
    window.destroy()
    del window
    wait(lambda: False, 0.1)
    gc.collect()
    assert [r() for r in gone] == [None, None, None]
