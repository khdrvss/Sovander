"""Links on the page view and the web-link dialog (task 4.2; Requirements 3.11, 9.3-9.5).

No test reaches a real browser: every dialog gets a Portal whose D-Bus proxy is a fake.
"""

import gc
import weakref

import gi
import pytest

from sovander import i18n, links
from sovander.links import LinkDialog, Links, Portal, confirm_and_open
from sovander.views.page_view import PageView

gi.require_version("Gtk", "4.0")
from gi.repository import GLib, Gtk  # noqa: E402

try:
    import pymupdf as fitz
except ImportError:
    import fitz

pytestmark = pytest.mark.gui

WEB = "https://example.com/a?b=1"
WEB_RECT, PAGE_RECT, SCRIPT_RECT = (72, 200, 272, 230), (72, 260, 272, 290), (72, 320, 272, 350)


@pytest.fixture(autouse=True)
def english(monkeypatch):
    monkeypatch.setattr(i18n, "_language", None)
    monkeypatch.setattr(i18n, "_listeners", [])
    i18n.set_language("en")


class FakeProxy:
    def __init__(self, owner=":1.42", error=None):
        self.owner, self.error, self.calls = owner, error, []

    def get_name_owner(self):
        return self.owner

    def call(self, method, params, flags, timeout, cancellable, callback, *user_data):
        self.calls.append((method, params.get_type_string(), params.unpack()))
        callback(self, object(), *user_data)

    def call_finish(self, _result):
        if self.error:
            raise GLib.Error(self.error)


def fake_portal(proxy=None, error=None):
    made = []

    def connect(done):
        made.append(1)
        done(proxy, error)

    portal = Portal(connect)
    portal.made = made
    return portal


@pytest.fixture(autouse=True)
def no_real_portal(monkeypatch):
    """A dialog made without a portal must not reach the session bus either."""
    monkeypatch.setattr(links, "_connect_session", lambda done: done(FakeProxy(), None))


def linked_pdf() -> bytes:
    doc = fitz.open()
    for i in range(8):
        doc.new_page().insert_text((72, 72), f"Page {i + 1}")
    page = doc[0]
    page.insert_link({"kind": fitz.LINK_URI, "from": fitz.Rect(WEB_RECT), "uri": WEB})
    page.insert_link(
        {"kind": fitz.LINK_GOTO, "from": fitz.Rect(PAGE_RECT), "page": 5, "to": fitz.Point(0, 0)}
    )
    page.insert_link({"kind": fitz.LINK_URI, "from": fitz.Rect(SCRIPT_RECT), "uri": "javascript:alert(1)"})
    return doc.tobytes()


@pytest.fixture
def linked(viewer):
    viewer.load(linked_pdf())
    seen = []
    return viewer, Links(viewer, on_external=seen.append), seen


def centre(view, rect):
    r = view.page_rect_to_widget(0, rect)
    return r.get_x() + r.get_width() / 2, r.get_y() + r.get_height() / 2


def clipboard_text(widget, wait):
    out = []
    widget.get_clipboard().read_text_async(None, lambda c, r: out.append(c.read_text_finish(r)))
    assert wait(lambda: out)
    return out[0]


def link_dialogs():
    return [w for w in Gtk.Window.list_toplevels() if isinstance(w, LinkDialog) and w.get_visible()]


# Following links (3.11)


def test_internal_link_moves_to_its_page(linked):
    view, handler, seen = linked
    assert handler.activate_at(*centre(view, PAGE_RECT))
    assert view.current_page == 5 and seen == []


def test_web_link_is_never_opened_directly(linked):
    view, handler, seen = linked
    assert handler.activate_at(*centre(view, WEB_RECT))
    assert seen == [WEB] and view.current_page == 0


def test_other_schemes_also_go_to_the_dialog(linked):
    view, handler, seen = linked
    handler.activate_at(*centre(view, SCRIPT_RECT))
    assert seen == ["javascript:alert(1)"]


def test_a_click_outside_links_does_nothing(linked):
    view, handler, seen = linked
    assert not handler.activate_at(*centre(view, (400, 600, 401, 601)))
    assert handler._on_released(None, 2, *centre(view, PAGE_RECT)) is None  # a double click is not a jump
    assert seen == [] and view.current_page == 0


def test_pointer_cursor_and_address_over_links(linked):
    view, handler, _seen = linked
    handler.hover(*centre(view, WEB_RECT))
    assert view.get_cursor() is not None and view.get_cursor().get_name() == "pointer"
    assert view.get_tooltip_text() == WEB
    handler.hover(*centre(view, PAGE_RECT))
    assert view.get_cursor().get_name() == "pointer" and view.get_tooltip_text() is None
    handler.hover(*centre(view, (400, 600, 401, 601)))
    assert view.get_cursor() is None
    handler.hover(-1, -1)
    assert view.get_cursor() is None


def test_web_link_by_default_asks_in_a_dialog_over_the_window(viewer, window, wait):
    viewer.load(linked_pdf())
    handler = Links(viewer)
    handler.activate_at(*centre(viewer, WEB_RECT))
    assert wait(lambda: link_dialogs())
    (dialog,) = link_dialogs()
    assert dialog.get_transient_for() is window and dialog.get_modal()
    assert dialog.address_label.get_text() == WEB and dialog.result is None
    dialog.cancel()


def test_links_follow_a_new_document(linked, make_pdf):
    view, handler, _seen = linked
    assert len(handler.links(0)) == 3
    view.load(make_pdf(2))
    assert handler.links(0) == []


# The dialog (9.3-9.5)


def test_dialog_shows_the_full_address_and_the_site(window):
    dialog = confirm_and_open(window, WEB, fake_portal(FakeProxy()))
    assert dialog.heading.get_text() == "Open this web address?"
    assert dialog.address_label.get_text() == WEB and dialog.address_label.get_selectable()
    assert "Site: example.com" in dialog.note.get_text()
    assert dialog.open_button.get_visible() and dialog.copy_button.get_visible()
    dialog.cancel()


def test_open_hands_the_address_to_the_portal(window):
    proxy = FakeProxy()
    dialog = confirm_and_open(window, WEB, fake_portal(proxy))
    finished = []
    dialog.connect("finished", lambda _d, result: finished.append(result))
    dialog.open()
    assert proxy.calls == [("OpenURI", "(ssa{sv})", ("", WEB, {}))]
    assert finished == ["opened"] and dialog.result == "opened"


def test_look_alike_site_is_shown_in_punycode(window):
    dialog = confirm_and_open(window, "https://аpple.com/login", fake_portal(FakeProxy()))
    assert dialog.address_label.get_text() == "https://xn--pple-43d.com/login"
    assert "аpple.com" in dialog.note.get_text() and "xn--pple-43d.com" in dialog.note.get_text()
    dialog.cancel()


def test_address_is_plain_text_not_markup(window):
    uri = "https://example.com/<b>x</b>&amp;"
    dialog = confirm_and_open(window, uri, fake_portal(FakeProxy()))
    assert dialog.address_label.get_text() == uri and not dialog.address_label.get_use_markup()
    dialog.cancel()


def test_copy_puts_the_address_on_the_clipboard(window, wait):
    dialog = confirm_and_open(window, WEB, fake_portal(FakeProxy()))
    dialog.copy()
    assert dialog.result == "copied"
    assert clipboard_text(window, wait) == WEB


def test_without_the_portal_only_copy_is_offered(window):
    portal = fake_portal(FakeProxy(owner=None))
    dialog = confirm_and_open(window, WEB, portal)
    assert not dialog.open_button.get_visible()
    assert dialog.heading.get_text() == "Copy this address?"
    assert "desktop portal is not available" in dialog.note.get_text()
    dialog.open()
    assert dialog.result is None
    dialog.cancel()


def test_portal_failure_keeps_the_dialog_with_copy(window, wait):
    proxy = FakeProxy(error="Access denied")
    dialog = confirm_and_open(window, WEB, fake_portal(proxy))
    dialog.open()
    assert len(proxy.calls) == 1 and dialog.result is None
    assert not dialog.open_button.get_visible() and dialog.copy_button.get_sensitive()
    assert "Access denied" in dialog.note.get_text()
    dialog.copy()
    assert clipboard_text(window, wait) == WEB


@pytest.mark.parametrize(
    "uri", ["javascript:alert(1)", "file:///etc/passwd", "smb://server/share", "other.pdf"]
)
def test_other_schemes_are_copy_only_and_never_reach_the_portal(window, uri):
    portal = fake_portal(FakeProxy())
    dialog = confirm_and_open(window, uri, portal)
    assert not dialog.open_button.get_visible() and dialog.heading.get_text() == "Copy this address?"
    assert "Only web and email addresses" in dialog.note.get_text()
    dialog.open()
    assert portal.made == [] and dialog.result is None
    dialog.cancel()


def test_cancel_and_escape_and_closing_cancel(window):
    for close in ("cancel", "escape", "close"):
        dialog = confirm_and_open(window, WEB, fake_portal(FakeProxy()))
        finished = []
        dialog.connect("finished", lambda _d, result, finished=finished: finished.append(result))
        if close == "escape":
            (shortcut,) = shortcuts(dialog, "Escape")
            assert shortcut.get_action().activate(Gtk.ShortcutActionFlags.EXCLUSIVE, dialog, None)
        else:
            getattr(dialog, close)()
        assert finished == ["cancelled"], close


def shortcuts(widget, trigger):
    found = []
    controllers = widget.observe_controllers()
    for i in range(controllers.get_n_items()):
        controller = controllers.get_item(i)
        if isinstance(controller, Gtk.ShortcutController):
            for j in range(controller.get_n_items()):
                shortcut = controller.get_item(j)
                if shortcut.get_trigger().to_string() == trigger:
                    found.append(shortcut)
    return found


def test_closed_view_is_freed_with_its_links(wait):
    # Handlers kept from C must not hold the view, or every closed document would stay in memory
    window = Gtk.Window()
    view = PageView()
    window.set_child(Gtk.ScrolledWindow(child=view))
    view.load(linked_pdf())
    window.present()
    wait(lambda: False, 0.2)
    handler = Links(view)
    handler.hover(*centre(view, WEB_RECT))
    gone = [weakref.ref(view), weakref.ref(handler)]
    del view, handler
    window.destroy()
    del window
    wait(lambda: False, 0.1)
    gc.collect()
    assert [r() for r in gone] == [None, None]
