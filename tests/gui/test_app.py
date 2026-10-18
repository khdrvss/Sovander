"""The application shell: windows, the right view per file, header controls, keyboard, lifetime (task 5.1).

Requirements 1.1-1.4, 1.6, 3.1, 3.2, 3.8, 3.9, 7.1, 7.2, 8.2, 9.7, 11.1, 11.2, 12.2-12.4.
"""

import faulthandler
import gc
import itertools
import signal
import sys
import threading
import weakref
import zipfile
from pathlib import Path
from types import SimpleNamespace

import gi
import pytest

gi.require_version("Adw", "1")
from gi.repository import Adw, Gdk, Gio, GLib, Gtk  # noqa: E402

from sovander import app as app_module  # noqa: E402
from sovander import i18n, offline  # noqa: E402
from sovander.app import ACCELS, SovanderApp  # noqa: E402
from sovander.document_context import Context  # noqa: E402
from sovander.engine import client  # noqa: E402
from sovander.engine.process import Engine, EngineTimeout  # noqa: E402
from sovander.open_document import Source  # noqa: E402
from sovander.store import Store  # noqa: E402
from sovander.views.image_view import ImageView  # noqa: E402
from sovander.views.page_view import PageView  # noqa: E402
from sovander.views.text_view import SHOWN_AS_TEXT, TextView  # noqa: E402
from sovander.window import SovanderWindow  # noqa: E402

pytestmark = pytest.mark.gui

_ids = itertools.count()


@pytest.fixture(autouse=True)
def english(monkeypatch):
    """English for the test; the language and listeners before it come back afterwards."""
    monkeypatch.setattr(i18n, "_language", None)
    monkeypatch.setattr(i18n, "_translation", i18n._translation)
    monkeypatch.setattr(i18n, "_listeners", [])
    i18n.set_language("en")


@pytest.fixture(autouse=True)
def light_afterwards():
    """A started Adw.Application makes the whole process follow the desktop's colour scheme (12.1).

    On a desktop that prefers dark, every later test would see dark widgets; pixel tests elsewhere
    expect GTK's light default. DEFAULT would follow the desktop again, so light is forced back.
    """
    yield
    Adw.StyleManager.get_default().set_color_scheme(Adw.ColorScheme.FORCE_LIGHT)


@pytest.fixture
def app(wait):
    store = Store()
    store.update_settings(language="en")
    application = SovanderApp(application_id=f"io.github.khdrvss.Test{next(_ids)}", store=store,
                              warm_engine=False, unique=False)
    assert application.register(None)
    yield application
    for window in application.get_windows():
        window.destroy()
    wait(lambda: False, 0.05)


@pytest.fixture
def open_file(app, fixture_file, wait):
    """open_file(name) -> its window, once the document page has left the loading state."""

    def run(name, settle=lambda page: page.state != "loading"):
        source = Source.from_path(fixture_file(name)) if "/" not in name else Source.from_path(name)
        (window,) = app.open_sources([source])
        if not wait(lambda: settle(window.page), 15):
            # Where the open is stuck, in the captured stderr: every thread, and whether this thread can
            # still run the main loop that delivers the open job's result
            faulthandler.dump_traceback(all_threads=True)
            context = GLib.MainContext.default()
            acquired = context.acquire()
            if acquired:
                context.release()
            pytest.fail(f"still {window.page.state}; main context acquirable: {acquired}, "
                        f"pending: {context.pending()}")
        return window

    return run


def freed(ref, wait) -> bool:
    for _ in range(5):
        wait(lambda: False, 0.1)
        gc.collect()
    return ref() is None


# The right view for each file (2, 7, 8.2)


def test_pdf_shows_in_a_page_view_with_name_and_page_counter(open_file, wait):
    window = open_file("six-pages.pdf")
    assert isinstance(window.page.view, PageView) and window.page.state == "ready"
    assert window.get_title() == "six-pages.pdf" == window.title_widget.get_title()
    assert window.page_counter == "1 / 6"
    window.goto_entry.set_text("4")
    window.goto_entry.emit("activate")
    assert wait(lambda: window.page_counter == "4 / 6")
    assert window.page.view.current_page == 3


@pytest.mark.parametrize("name, view_type, note", [
    ("plain.txt", TextView, None),
    ("unknown.xyz", TextView, SHOWN_AS_TEXT),
    ("tiny.png", ImageView, None),
    ("notes.md", "WebView", None),
    ("almanac.epub", "WebView", None),
])
def test_each_kind_gets_its_view(open_file, name, view_type, note):
    window = open_file(name)
    view = window.page.view
    assert window.page.state == "ready"
    assert type(view).__name__ == view_type if isinstance(view_type, str) else isinstance(view, view_type)
    assert getattr(view, "note", None) == note
    assert window.page_counter == ""  # pages are counted for paged documents only


def test_unknown_binary_says_unsupported_and_offers_text(open_file, wait):
    window = open_file("opaque.xyz")
    page = window.page
    assert page.state == "unsupported" and page.view is None
    button = page._slot.get_first_child().get_child()
    assert button.get_label() == "View as Text"
    button.emit("clicked")
    assert isinstance(page.view, TextView) and page.view.note == SHOWN_AS_TEXT


def test_zip_entries_open_from_memory_in_windows_of_their_own(open_file, app, wait):
    archive_view = pytest.importorskip("sovander.views.archive_view")
    window = open_file("archive.zip")
    view = window.page.view
    assert isinstance(view, archive_view.ArchiveView)
    (entry,) = [e for e in view.shown() if e.name == "plain.txt"]
    view.open_entry(entry)
    assert wait(lambda: len(app.get_windows()) == 2)
    (entry_window,) = [w for w in app.get_windows() if w is not window]
    assert entry_window.page.source.path is None and entry_window.page.source.name == "plain.txt"
    assert isinstance(entry_window.page.view, TextView) and "Plain text" in entry_window.page.view.text


def test_an_entry_read_after_its_window_closed_opens_nothing(open_file, app, wait, monkeypatch):
    archive_view = pytest.importorskip("sovander.views.archive_view")
    errors = []
    monkeypatch.setattr(sys, "excepthook", lambda *exc: errors.append(exc))  # where callback errors go
    window = open_file("archive.zip")
    view = window.page.view
    assert isinstance(view, archive_view.ArchiveView)
    (entry,) = [e for e in view.shown() if e.name == "plain.txt"]
    view.open_entry(entry)
    window.close()
    assert wait(lambda: not view.busy)
    assert errors == [] and app.get_windows() == []


def test_missing_file_names_the_file_and_the_app_keeps_going(open_file, app, tmp_path):
    window = open_file(str(tmp_path / "gone.pdf"))
    assert window.page.state == "error"
    assert window.page.error_text == "Cannot open “gone.pdf”: The file does not exist"
    assert isinstance(open_file("six-pages.pdf").page.view, PageView)
    assert len(app.get_windows()) == 2


def test_a_folder_is_not_opened_as_a_file(open_file, tmp_path):
    assert open_file(str(tmp_path)).page.state == "error"


def test_office_file_without_the_engine_says_how_to_install_it(open_file, app, monkeypatch, tmp_path):
    monkeypatch.setattr("sovander.engine.process.find_soffice", lambda: None)
    # an engine of its own: the app-wide one must not be left without LibreOffice for later tests
    app.context = Context(app.store, Engine(soffice="/nonexistent/soffice"), tmp_path / "renders")
    page = open_file("report.docx", settle=lambda p: p.state not in ("new", "loading")).page
    assert page.state == "error" and page.error_text == "Install the document engine"


# Passwords (3.8, 3.9)


def test_pdf_password_is_asked_wrong_one_said_right_one_opens(open_file, wait):
    page = open_file("encrypted.pdf").page
    assert page.state == "password" and not page.wrong_password
    page.password_entry.set_text("nope")
    page.password_entry.emit("activate")
    assert page.state == "password" and page.wrong_password
    page.password_entry.set_text("gander")
    page.submit_password()
    assert page.state == "ready" and isinstance(page.view, PageView) and page.view.page_count >= 1


# Windows (1.2, 1.3, 11.1)


def test_one_window_per_document_and_the_same_file_only_once(app, fixture_file):
    first = app.open_sources([Source.from_path(fixture_file("six-pages.pdf"))])[0]
    second = app.open_sources([Source.from_path(fixture_file("plain.txt"))])[0]
    again = app.open_sources([Source.from_path(fixture_file("six-pages.pdf"))])[0]
    assert first is again and first is not second
    assert len(app.get_windows()) == 2


def test_started_without_files_shows_the_home_screen(app):
    app.activate()
    (window,) = app.get_windows()
    assert window.page is None and window.home is not None
    app.activate()
    assert len(app.get_windows()) == 1  # the home window is brought forward, not doubled


def test_files_given_on_the_command_line_open(app, fixture_file):
    files = [Gio.File.new_for_path(str(fixture_file(n))) for n in ("six-pages.pdf", "plain.txt")]
    app.do_open(files, len(files), "")
    assert sorted(w.get_title() for w in app.get_windows()) == ["plain.txt", "six-pages.pdf"]


def test_drop_target_takes_files_into_the_empty_window_then_new_ones(app, fixture_file):
    app.activate()
    (home,) = app.get_windows()
    assert Gdk.FileList.__gtype__ in home.drop_target.get_gtypes()
    if not hasattr(Gdk.FileList, "new_from_list"):
        pytest.skip("Gdk.FileList.new_from_list needs GTK 4.8 or newer (Ubuntu 22.04 has 4.6)")
    files = [Gio.File.new_for_path(str(fixture_file(n))) for n in ("six-pages.pdf", "tiny.png")]
    assert home._on_drop(home.drop_target, Gdk.FileList.new_from_list(files), 10, 10)
    assert home.page is not None and home.get_title() == "six-pages.pdf"
    assert sorted(w.get_title() for w in app.get_windows()) == ["six-pages.pdf", "tiny.png"]


# Header and keyboard (3.3, 3.4, 6.1, 12.2-12.4)


def test_every_action_is_reachable_from_the_keyboard(open_file):
    window = open_file("six-pages.pdf")
    in_menu = menu_actions(window.menu_button.get_menu_model())  # the menu the user sees, submenus too
    names = {f"win.{name}" for name in window.list_actions()}
    reachable = in_menu | set(ACCELS)
    assert names <= reachable, names - reachable
    assert window.menu_button.get_primary()  # F10 opens the menu
    assert ACCELS["win.fullscreen"] == ["F11"] and ACCELS["win.open"] == ["<Control>o"]


def test_f11_toggles_full_screen_and_escape_leaves_it(open_file, app):
    window = open_file("six-pages.pdf")
    assert app.get_accels_for_action("win.fullscreen") == ["F11"]
    window.activate_action("win.fullscreen", None)
    assert window.fullscreen_requested
    assert window._on_key(None, Gdk.KEY_Escape, 0, 0)
    assert not window.fullscreen_requested
    window.activate_action("win.fullscreen", None)
    window.activate_action("win.fullscreen", None)
    assert not window.fullscreen_requested


def test_zoom_and_fit_buttons_drive_the_page_view(open_file, wait):
    window = open_file("six-pages.pdf")
    view = window.page.view
    assert wait(lambda: view.get_width() > 0)
    before = view.zoom
    window.activate_action("win.zoom-in", None)
    assert view.zoom > before
    window.activate_action("win.fit-width", None)
    assert view.sizing == "fit-width"
    window.activate_action("win.fit-page", None)
    assert view.sizing == "fit-page"


def test_find_opens_the_bar_on_the_view(open_file):
    window = open_file("six-pages.pdf")
    window.activate_action("win.find", None)
    assert window.find_bar.get_search_mode()
    window.find_bar.entry.set_text("tenancy")
    window.find_bar.search()
    assert window.find_bar._backend is window.page.find_backend() is not None


def test_night_mode_reaches_every_window_and_is_kept(open_file, app):
    pdf, other = open_file("six-pages.pdf"), open_file("forty-pages.pdf")
    pdf.activate_action("win.night", None)
    assert pdf.page.view.night and other.page.view.night
    assert app.store.settings.night_mode
    assert open_file("embedded-font.pdf").page.view.night


def test_print_reaches_the_view_and_a_view_without_printing_says_so(open_file, monkeypatch):
    printing = pytest.importorskip("sovander.print")
    window = open_file("six-pages.pdf")
    printed = []
    monkeypatch.setattr(printing, "print_view", lambda parent, view: printed.append((parent, view)))
    window.activate_action("win.print", None)
    assert printed == [(window, window.page.view)]

    def refuse(_parent, _view):
        raise printing.NotPrintable()

    monkeypatch.setattr(printing, "print_view", refuse)
    toasts = []
    monkeypatch.setattr(window, "show_toast", toasts.append)
    window.activate_action("win.print", None)
    assert toasts == ["This document cannot be printed"]


def test_about_shows_the_offline_status(open_file, app, monkeypatch):
    about = pytest.importorskip("sovander.about")
    shown = []
    monkeypatch.setattr(about, "show_about", lambda parent, status=None: shown.append((parent, status)))
    window = open_file("plain.txt")
    window.activate_action("win.about", None)
    assert shown == [(window, app.offline_status)]


def test_present_is_offered_for_slides_only(open_file):
    window = open_file("six-pages.pdf")
    assert not window.lookup_action("present").get_enabled() and not window.present_button.get_visible()


# Offline banner (9.7)


def test_unprotected_shows_a_banner_that_can_be_dismissed(open_file, app):
    window = open_file("six-pages.pdf")
    assert app.offline_status is offline.OfflineStatus.UNPROTECTED  # tests run unconfined
    assert window.banner.get_reveal_child()
    assert window.banner_label.get_text() == "Offline protection is off: the network is not blocked"
    window.dismiss_button.emit("clicked")
    assert not window.banner.get_reveal_child()


def test_launcher_without_confinement_counts_an_undecided_check_as_off(monkeypatch):
    monkeypatch.setattr(offline, "check", lambda: offline.OfflineStatus.UNKNOWN)
    monkeypatch.setenv("SOVANDER_CONFINEMENT", "none")
    assert app_module.offline_status() is offline.OfflineStatus.UNPROTECTED
    monkeypatch.setenv("SOVANDER_CONFINEMENT", "apparmor")
    assert app_module.offline_status() is offline.OfflineStatus.UNKNOWN


def test_run_quits_cleanly_on_interrupt_terminate_and_hangup(monkeypatch):
    """The engine runs in a session of its own: an app killed outright would leave it running."""
    hooked, quits = [], []
    monkeypatch.setattr(GLib, "unix_signal_add",
                        lambda _priority, signum, handler, app: hooked.append((signum, handler, app)))
    monkeypatch.setattr(SovanderApp, "run", lambda self, argv: 0)
    assert app_module.run(["sovander"]) == 0
    assert [h[0] for h in hooked] == [signal.SIGINT, signal.SIGTERM, signal.SIGHUP]
    _signum, handler, application = hooked[1]
    monkeypatch.setattr(application, "quit", lambda: quits.append(True))
    assert handler(application) == GLib.SOURCE_CONTINUE and quits == [True]


# Recents and positions (11.1, 11.2)


def test_open_is_recorded_and_the_page_is_restored(open_file, app, fixture_file, wait):
    window = open_file("forty-pages.pdf")
    assert [r.display_name for r in app.store.recents()] == ["forty-pages.pdf"]
    window.page.view.go_to(12)
    assert wait(lambda: window.page.view.current_page == 12)
    window.close()
    wait(lambda: False, 0.1)
    again = open_file("forty-pages.pdf")
    assert wait(lambda: again.page.view.current_page == 12)


# Lifetime


@pytest.mark.parametrize("name", ["six-pages.pdf", "plain.txt", "tiny.png", "archive.zip", "encrypted.pdf"])
def test_closing_a_window_frees_its_view(open_file, wait, name):
    window = open_file(name)
    page = weakref.ref(window.page)
    view = weakref.ref(window.page.view) if window.page.view is not None else None
    window.close()
    del window
    assert freed(page, wait)
    assert view is None or freed(view, wait)


def test_windows_survive_a_language_change(open_file, monkeypatch):
    window = open_file("opaque.xyz")
    i18n.set_language("ru")
    assert window.page.state == "unsupported"
    assert window.page._slot.get_first_child().get_child().get_label() != "View as Text"
    i18n.set_language("en")
    assert isinstance(window, SovanderWindow) and GLib.MainContext.default() is not None
    assert Gtk.Window.list_toplevels()


# The office pipeline over a stand-in engine (2.2, 3.8-3.10, 5.7, 10.2): every step, no LibreOffice


class StandIn:
    """Plays client.convert: page 1, then waits for ``release`` before the whole document."""

    def __init__(self, make_pdf):
        self.make_pdf, self.calls, self.release = make_pdf, [], threading.Event()
        self.password = self.failure = None
        self.damaged = False
        self.engine = SimpleNamespace(alive=True, version="Stand-in 1.0", soffice="/bin/true")

    def convert(self, data, kind, password=None, engine=None):
        self.calls.append((kind, password))
        if self.password is not None and password != self.password:
            raise (client.WrongPassword if password else client.NeedsPassword)("password")
        yield client.PdfChunk.of(self.make_pdf(1), final=False, damaged=self.damaged)
        self.release.wait(10)
        if self.failure is not None:
            raise self.failure
        yield client.PdfChunk.of(self.make_pdf(5), final=True, damaged=self.damaged)


@pytest.fixture
def stand_in(app, make_pdf, monkeypatch, tmp_path):
    fake = StandIn(make_pdf)
    monkeypatch.setattr(client, "convert", fake.convert)
    app.context = Context(app.store, fake.engine, tmp_path / "renders")
    yield fake
    fake.release.set()


def first_page(page) -> bool:
    return isinstance(page.view, PageView) and page.view.page_count >= 1


def test_page_one_shows_with_a_loading_row_then_the_whole_document(open_file, stand_in, wait):
    page = open_file("report.docx", settle=first_page).page
    assert (page.view.page_count, page.view.loading_more, page.state) == (1, True, "ready")
    stand_in.release.set()
    assert wait(lambda: page.view.page_count == 5 and not page.view.loading_more)


def test_second_open_comes_from_the_render_cache(open_file, stand_in, wait):
    stand_in.release.set()
    window = open_file("report.docx", settle=lambda p: first_page(p) and p.view.page_count == 5)
    window.close()
    again = open_file("report.docx", settle=first_page).page
    assert (again.view.page_count, again.view.loading_more) == (5, False)
    assert len(stand_in.calls) == 1


def test_office_password_is_asked_and_never_cached(open_file, stand_in, wait):
    stand_in.password = "right"
    stand_in.release.set()
    page = open_file("report.docx").page
    assert page.state == "password" and not page.wrong_password
    page.password_entry.set_text("wrong")
    page.submit_password()
    assert wait(lambda: page.wrong_password and page.state == "password")
    page.password_entry.set_text("right")
    page.submit_password()
    assert wait(lambda: first_page(page) and page.view.page_count == 5)
    assert stand_in.calls[-1] == (client.TEXT, "right")
    assert not any((page.context._cache_root or Path("/nonexistent")).rglob("*.pdf"))


def test_damaged_file_shows_what_was_read_with_a_notice(open_file, stand_in):
    stand_in.damaged = True
    page = open_file("report.docx", settle=first_page).page
    assert page.notes == ["This file is damaged. Showing the readable part."]


def test_macros_are_noted_and_not_run(open_file, stand_in, fixture_file, tmp_path, wait):
    docm = tmp_path / "macro.docm"
    with zipfile.ZipFile(fixture_file("report.docx")) as source, zipfile.ZipFile(docm, "w") as out:
        for item in source.infolist():
            out.writestr(item, source.read(item))
        out.writestr("word/vbaProject.bin", b"\xd0\xcf\x11\xe0 not run")
    page = open_file(str(docm), settle=first_page).page
    assert wait(lambda: page.notes == ["This document contains macros. They were not run."])


def test_failure_after_page_one_keeps_page_one(open_file, stand_in, wait):
    stand_in.failure = EngineTimeout("slow")
    page = open_file("report.docx", settle=first_page).page
    stand_in.release.set()
    assert wait(lambda: not page.view.loading_more)
    assert page.state == "ready" and page.view.page_count == 1
    assert page.notes == ["The document took too long to prepare and was stopped"]


def test_closing_during_a_conversion_frees_the_window(open_file, stand_in, wait):
    window = open_file("report.docx", settle=first_page)
    page, view = weakref.ref(window.page), weakref.ref(window.page.view)
    window.close()
    del window
    assert freed(page, wait) and freed(view, wait)  # the worker is still waiting for the rest
    stand_in.release.set()
    wait(lambda: False, 0.2)


def test_slides_offer_presentation_mode(open_file, stand_in, app, wait):
    stand_in.release.set()
    window = open_file("deck.pptx", settle=lambda p: first_page(p) and p.view.page_count == 5)
    assert window.page.view.mode == "slides"
    assert window.page.view.sizing == "fit-page"  # a whole slide in view, not cut at its printed size
    assert window.lookup_action("present").get_enabled() and window.present_button.get_visible()
    assert app.get_accels_for_action("win.present") == ["F5"]
    window.activate_action("win.present", None)
    shows = [w for w in Gtk.Window.list_toplevels() if type(w).__name__ == "PresentWindow"]
    assert len(shows) == 1
    shows[0].close()


def test_language_menu_switches_every_window_and_is_kept(open_file):
    first, second = open_file("opaque.xyz"), open_file("plain.txt")
    assert first.lookup_action("language").get_state().get_string() == "en"
    first.activate_action("win.language", GLib.Variant.new_string("ru"))
    assert i18n.language() == "ru"
    assert first.get_application().store.settings.language == "ru"  # remembered for the next start
    assert second.lookup_action("language").get_state().get_string() == "ru"
    assert "Язык" in menu_labels(second.menu_button.get_menu_model())  # the other window's menu too
    first.activate_action("win.language", GLib.Variant.new_string("system"))
    assert first.get_application().store.settings.language is None


def menu_labels(model) -> list:
    """Every item label in a menu model, sections and submenus included."""
    labels = []
    for i in range(model.get_n_items()):
        label = model.get_item_attribute_value(i, "label", None)
        if label is not None:
            labels.append(label.get_string())
        for link in ("section", "submenu"):
            child = model.get_item_link(i, link)
            if child is not None:
                labels += menu_labels(child)
    return labels


def menu_actions(model) -> set:
    """Every action name a menu model can trigger, without targets, sections and submenus included."""
    actions = set()
    for i in range(model.get_n_items()):
        action = model.get_item_attribute_value(i, "action", None)
        if action is not None:
            actions.add(action.get_string())
        for link in ("section", "submenu"):
            child = model.get_item_link(i, link)
            if child is not None:
                actions |= menu_actions(child)
    return actions
