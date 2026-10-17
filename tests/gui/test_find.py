"""Find in document: matching, the page and text backends, the search bar (task 4.7, Requirement 6)."""

import gc
import weakref
from pathlib import Path

import gi
import pytest

from sovander import i18n
from sovander.find import FindBackend, FindBar, PageViewBackend, TextViewBackend, fold, page_hits
from sovander.views.page_view import PageView
from sovander.views.selection import HighlightLayer
from sovander.views.text_view import TextView

gi.require_version("Gdk", "4.0")
gi.require_version("Gtk", "4.0")
from gi.repository import Gdk, Gtk  # noqa: E402

try:
    import pymupdf as fitz
except ImportError:
    import fitz

pytestmark = pytest.mark.gui

DEJAVU = Path("/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf")
NEEDLES = {3: "a needle here", 10: "needle and Needle", 25: "the NEEDLE"}  # 4 matches ignoring case


@pytest.fixture(autouse=True)
def english(monkeypatch):
    monkeypatch.setattr(i18n, "_language", None)
    monkeypatch.setattr(i18n, "_listeners", [])
    i18n.set_language("en")


def needle_pdf(pages=30) -> bytes:
    doc = fitz.open()
    for i in range(pages):
        page = doc.new_page()
        page.insert_text((72, 72), f"Page {i + 1} filler text", fontsize=14)
        if i in NEEDLES:
            page.insert_text((72, 300), NEEDLES[i], fontsize=14)
    return doc.tobytes()


_DOCS = []  # PyMuPDF < 1.24 (Ubuntu's) orphans a page whose document was collected: keep them


def one_page(*lines, font=None):
    doc = fitz.open()
    page = doc.new_page()
    kwargs = {}
    if font:
        page.insert_font(fontname="dv", fontfile=str(font))
        kwargs["fontname"] = "dv"
    for n, line in enumerate(lines):
        page.insert_text((72, 100 + 20 * n), line, fontsize=12, **kwargs)
    _DOCS.append(fitz.open(stream=doc.tobytes(), filetype="pdf"))
    return _DOCS[-1][0]


@pytest.fixture
def searching(viewer, window, wait):
    viewer.load(needle_pdf())
    scrolled = viewer.get_parent()
    window.set_child(None)
    layer = HighlightLayer(viewer)
    bar = FindBar()
    box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL)
    box.append(bar)
    box.append(layer.wrap(scrolled))
    scrolled.set_vexpand(True)
    window.set_child(box)
    assert wait(lambda: viewer.get_height() > 0 and layer.get_width() > 0)
    return viewer, layer, PageViewBackend(viewer, layer), bar


def finish(backend, wait):
    assert wait(lambda: not backend.busy)
    return backend.index, backend.total


def visible(view, page, rects):
    x0, y0 = min(r[0] for r in rects), min(r[1] for r in rects)
    x1, y1 = max(r[2] for r in rects), max(r[3] for r in rects)
    r = view.page_rect_to_widget(page, (x0, y0, x1, y1))
    return 0 <= r.get_y() and r.get_y() + r.get_height() <= view.get_height()


# Matching on a page


@pytest.mark.skipif(not DEJAVU.exists(), reason="needs DejaVu Sans for Cyrillic")
def test_case_is_ignored_for_russian_too():
    page = one_page("Привет мир ПРИВЕТ привет", font=DEJAVU)
    assert len(page.search_for("привет")) == 1  # why page_hits exists: MuPDF folds Latin letters only
    assert len(page_hits(page, "привет")) == 3
    assert len(page_hits(page, "Привет", case=True)) == 1


def test_a_match_across_a_line_break_is_one_match_with_two_boxes():
    page = one_page("the end of the line says hello", "world starts the next one")
    (hit,) = page_hits(page, "hello   world")
    assert len(hit) == 2 and hit[0][1] < hit[1][1]


def test_match_boxes_cover_only_the_match():
    page = one_page("xx needle yy")
    ((box,),) = page_hits(page, "needle")
    (word,) = [w for w in page.get_text("words") if w[4] == "needle"]
    assert box == pytest.approx(word[:4], abs=0.5)


def test_fold_keeps_offsets():
    assert fold("ПРИВЕТ İstanbul") == "привет İstanbul"
    assert len(fold("İİ")) == 2


# The page view backend (6.2-6.6)


def test_counts_every_match_and_steps_through_them(searching, wait):
    view, _layer, backend, _bar = searching
    assert backend.find("needle") == (-1, 0) and backend.busy
    assert finish(backend, wait) == (0, 4)
    assert visible(view, 3, backend.hits(3)[0])
    steps = [backend.find("needle") for _ in range(4)]
    assert steps == [(1, 4), (2, 4), (3, 4), (0, 4)]  # wraps after the last
    assert backend.find("needle", forward=False) == (3, 4)
    assert visible(view, 25, backend.hits(25)[0])


def test_search_starts_where_the_reader_is(searching, wait):
    view, _layer, backend, _bar = searching
    view.go_to(12)
    backend.find("needle")
    assert finish(backend, wait) == (3, 4)


def test_progress_reaches_the_listener(searching, wait):
    _view, _layer, backend, _bar = searching
    heard = []
    backend.watch(lambda *update: heard.append(update))
    backend.find("needle")
    finish(backend, wait)
    assert heard[0] == (-1, 0, True) and heard[-1] == (0, 4, False)


def test_case_matching(searching, wait):
    _view, _layer, backend, _bar = searching
    backend.find("needle", case=True)
    assert finish(backend, wait)[1] == 2
    backend.find("NEEDLE", case=True)
    assert finish(backend, wait)[1] == 1


def test_no_matches(searching, wait):
    _view, _layer, backend, _bar = searching
    backend.find("zebra")
    assert finish(backend, wait) == (-1, 0)
    assert backend.find("   ") == (-1, 0) and not backend.busy


def test_matches_are_highlighted_and_cleared(searching, wait, pixels):
    view, layer, backend, _bar = searching
    backend.find("needle")
    finish(backend, wait)
    (rect,) = backend.hits(3)[0]
    r = view.page_rect_to_widget(3, rect)
    x, y = int(r.get_x() + r.get_width() / 2), int(r.get_y() + r.get_height() / 2)
    red, green, blue, alpha = pixels(layer).pixel(x, y)
    assert alpha > 100 and red > green > blue  # the current match, orange
    backend.clear()
    snapshot = Gtk.Snapshot()
    layer.do_snapshot(snapshot)
    assert backend.hits(3) == [] and snapshot.to_node() is None  # nothing drawn any more


def test_a_new_document_is_searched_again(searching, wait, make_pdf):
    view, _layer, backend, _bar = searching
    backend.find("needle")
    finish(backend, wait)
    view.load(make_pdf(5))
    assert backend.hits(3) == []
    backend.find("needle")
    assert finish(backend, wait) == (-1, 0)
    backend.find("page")
    assert finish(backend, wait)[1] == 5


# The text view backend


def test_text_view_search(wait):
    view = TextView()
    view.load_bytes(b"alpha Beta beta\nBETA gamma beta")
    backend = TextViewBackend(view)
    assert backend.find("beta") == (0, 4)
    first, last = view.buffer.get_selection_bounds()
    assert (first.get_offset(), last.get_offset()) == (6, 10)
    assert [backend.find("beta")[0] for _ in range(4)] == [1, 2, 3, 0]
    assert backend.find("beta", forward=False) == (3, 4)
    assert backend.find("beta", case=True)[1] == 2
    assert view.buffer.get_iter_at_offset(6).has_tag(backend._tag) is False  # only "beta" matches now
    backend.clear()
    assert not view.buffer.get_iter_at_offset(11).has_tag(backend._tag)
    assert backend.find("zebra") == (-1, 0)


def test_text_view_counts_again_after_show_more():
    view = TextView(page_bytes=20)
    view.load_bytes(b"beta " * 10)
    backend = TextViewBackend(view)
    assert backend.find("beta") == (0, 4)
    view.show_more()
    assert backend.find("beta")[1] == 8


def test_a_plain_text_view_works_too():
    view = Gtk.TextView()
    view.get_buffer().set_text("one two one")
    assert TextViewBackend(view).find("ONE") == (0, 2)


# The search bar (6.1, 6.3, 6.4, 6.6)


def shortcut_action(widget, trigger):
    controllers = widget.observe_controllers()
    for i in range(controllers.get_n_items()):
        controller = controllers.get_item(i)
        if isinstance(controller, Gtk.ShortcutController):
            for j in range(controller.get_n_items()):
                shortcut = controller.get_item(j)
                if shortcut.get_trigger().to_string() == trigger:
                    return shortcut.get_action()
    raise AssertionError(f"no shortcut {trigger}")


def activate(window, trigger):
    return shortcut_action(window, trigger).activate(Gtk.ShortcutActionFlags.EXCLUSIVE, window, None)


def test_ctrl_f_opens_and_escape_closes(searching, window, wait):
    view, _layer, backend, bar = searching
    bar.set_backend(backend)
    bar.attach(window)
    view.grab_focus()
    assert not activate(window, "Escape")  # closed: Escape is left to others
    assert activate(window, "<Control>f")
    assert bar.get_search_mode()
    assert wait(lambda: window.get_focus() is not None and window.get_focus().is_ancestor(bar.entry))
    bar.entry.set_text("needle")
    assert wait(lambda: bar.status.get_text() == "1 of 4")
    assert activate(window, "Escape")
    assert not bar.get_search_mode() and backend.hits(3) == []
    assert wait(lambda: window.get_focus() is view)  # focus goes back to the document


def test_typing_shows_n_of_m_and_enter_steps(searching, wait):
    _view, _layer, backend, bar = searching
    bar.set_backend(backend)
    bar.open()
    bar.entry.set_text("needle")
    assert wait(lambda: bar.status.get_text() == "1 of 4")
    bar.next()
    assert bar.status.get_text() == "2 of 4"
    assert bar._on_entry_key(None, Gdk.KEY_Return, 0, Gdk.ModifierType.SHIFT_MASK)
    assert bar.status.get_text() == "1 of 4" and bar.result == (0, 4)
    assert bar._on_entry_key(None, Gdk.KEY_KP_Enter, 0, 0)
    assert bar.status.get_text() == "2 of 4"
    assert not bar._on_entry_key(None, Gdk.KEY_a, 0, 0)


def test_case_toggle_searches_again(searching, wait):
    _view, _layer, backend, bar = searching
    bar.set_backend(backend)
    bar.open()
    bar.entry.set_text("needle")
    assert wait(lambda: bar.result[1] == 4 and not backend.busy)
    bar.case_button.set_active(True)
    assert wait(lambda: bar.result[1] == 2 and not backend.busy)


def test_no_matches_is_said(searching, wait):
    _view, _layer, backend, bar = searching
    bar.set_backend(backend)
    bar.open()
    bar.entry.set_text("zebra")
    assert wait(lambda: bar.status.get_text() == "No matches")
    assert bar.entry.has_css_class("error") and not bar.next_button.get_sensitive()
    bar.entry.set_text("")
    assert wait(lambda: bar.status.get_text() == "" and not bar.entry.has_css_class("error"))


def test_labels_follow_the_language(searching, wait):
    _view, _layer, backend, bar = searching
    bar.set_backend(backend)
    bar.open()
    bar.entry.set_text("needle")
    assert wait(lambda: bar.status.get_text() == "1 of 4")
    i18n.set_language("ru")
    assert bar.status.get_text() == "1 из 4"
    assert bar.entry.get_property("placeholder-text") == "Найти в документе"
    assert bar.case_button.get_label() == "Учитывать регистр"


class WebLike:
    """A backend of the web view's shape: synchronous, no progress."""

    def __init__(self):
        self.calls, self.cleared = [], 0

    def find(self, query, forward=True, case=False):
        self.calls.append((query, forward, case))
        return len(self.calls) - 1, 5

    def clear(self):
        self.cleared += 1


def test_any_backend_of_the_same_shape_plugs_in(searching, wait):
    view, layer, backend, bar = searching
    web = WebLike()
    assert isinstance(web, FindBackend) and isinstance(backend, FindBackend)
    bar.set_backend(web)
    bar.open()
    bar.entry.set_text("owl")
    assert wait(lambda: bar.status.get_text() == "1 of 5")
    bar.previous()
    assert web.calls == [("owl", True, False), ("owl", False, False)]
    bar.close()  # GtkSearchBar also empties the entry, which clears again
    assert web.cleared >= 1 and bar.status.get_text() == ""


def test_closed_views_are_freed_with_their_search(wait):
    # Handlers kept from C must not hold the view, or every closed document would stay in memory
    window = Gtk.Window()
    view, text = PageView(), TextView()
    layer, bar = HighlightLayer(view), FindBar()
    box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL)
    box.append(bar)
    box.append(layer.wrap(Gtk.ScrolledWindow(child=view, vexpand=True)))
    box.append(text)
    window.set_child(box)
    view.load(needle_pdf(200))
    text.load_bytes(b"needle " * 50)
    window.present()
    wait(lambda: False, 0.2)
    pages, texts = PageViewBackend(view, layer), TextViewBackend(text)
    texts.find("needle")
    bar.attach(window)
    bar.set_backend(pages)
    bar.open()
    pages.find("needle")  # still searching when the window closes
    gone = [weakref.ref(o) for o in (view, text, layer, bar, pages, texts)]
    del view, text, layer, bar, pages, texts, box
    window.destroy()
    del window
    wait(lambda: False, 0.3)
    gc.collect()
    wait(lambda: False, 0.1)
    gc.collect()
    assert [r() for r in gone] == [None] * 6
