import codecs
import gc
import time
import weakref

import pytest

gi = pytest.importorskip("gi")
gi.require_version("Gtk", "4.0")
from gi.repository import GLib, Gtk  # noqa: E402

from sovander import i18n  # noqa: E402
from sovander.i18n import N_  # noqa: E402
from sovander.views import text_view  # noqa: E402
from sovander.views.text_view import (  # noqa: E402
    DEFAULT_PAGE_BYTES,
    SHOWN_AS_TEXT,
    TextView,
    detect_encoding,
)

pytestmark = pytest.mark.gui

RUSSIAN = "Съешь же ещё этих мягких французских булок, да выпей чаю.\n"


@pytest.fixture(autouse=True)
def english():
    i18n.set_language("en")
    yield
    i18n.set_language("en")


# Encodings (2.9)


@pytest.mark.parametrize("name", ["utf16le.txt", "utf16be.txt"])
def test_utf16_by_its_mark_and_the_mark_is_not_shown(fixture_file, name):
    view = TextView()
    view.load_path(fixture_file(name))
    assert view.encoding == "utf-16"
    assert view.text.startswith("Byte order marked text")
    assert "﻿" not in view.text


def test_utf8_without_a_mark(fixture_file):
    view = TextView()
    view.load_path(fixture_file("utf8.csv"))
    expected = fixture_file("utf8.csv").read_bytes().decode("utf-8")
    assert view.encoding == "utf-8" and view.text == expected


def test_latin1_is_not_read_as_utf8(fixture_file):
    view = TextView()
    view.load_path(fixture_file("latin1.csv"))
    assert "Café" in view.text and "Grüße" in view.text
    assert "�" not in view.text


def test_windows_cyrillic():
    view = TextView()
    view.load_bytes((RUSSIAN * 4).encode("cp1251"))
    assert view.encoding == "cp1251" and view.text == RUSSIAN * 4


@pytest.mark.parametrize(
    ("data", "expected"),
    [
        (codecs.BOM_UTF8 + b"x", "utf-8-sig"),
        (codecs.BOM_UTF32_LE + "x".encode("utf-32-le"), "utf-32"),
        (codecs.BOM_UTF16_BE + "x".encode("utf-16-be"), "utf-16"),
        ("ёж".encode()[:-1], "utf-8"),  # a sample cut inside its last letter
        (b"", "utf-8"),
        (b"plain ascii", "utf-8"),
    ],
)
def test_detect_encoding(data, expected):
    assert detect_encoding(data) == expected


def test_nul_bytes_do_not_cut_the_text():
    view = TextView()
    view.load_bytes(b"before\0after")
    assert view.text == "before␀after"


# Paging (7.3)


def test_default_page_is_five_megabytes(tmp_path):
    assert DEFAULT_PAGE_BYTES == 5 * 1000 * 1000
    path = tmp_path / "big.log"
    path.write_bytes(b"line of a log file\n" * 300_000)  # 5.7 MB
    view = TextView()
    view.load_path(path)
    assert view.shown_bytes == DEFAULT_PAGE_BYTES and view.has_more
    assert view._more_bar.get_revealed()
    assert view._more_label.get_text() == "Showing 5.0 MB of 5.7 MB"
    assert view.show_more() is True
    assert not view.has_more and not view._more_bar.get_revealed()
    assert len(view.text) == path.stat().st_size
    assert view.show_more() is False


def test_pages_split_inside_letters_join_up():
    data = RUSSIAN.encode("utf-8")
    view = TextView(page_bytes=7)  # Cyrillic letters are two bytes, so pages end mid-letter
    view.load_bytes(data)
    assert view.has_more and len(view.text) < len(RUSSIAN)
    while view.show_more():
        pass
    assert view.text == RUSSIAN


def test_utf16_pages_split_mid_character(fixture_file):
    data = fixture_file("utf16le.txt").read_bytes()
    view = TextView(page_bytes=5)
    view.load_bytes(data)
    while view.show_more():
        pass
    assert view.text == data.decode("utf-16")


def test_small_file_has_no_show_more(fixture_file):
    view = TextView()
    view.load_path(fixture_file("plain.txt"))
    assert not view.has_more and not view._more_bar.get_revealed()


def test_empty_file(tmp_path):
    (tmp_path / "empty.txt").write_bytes(b"")
    view = TextView()
    view.load_path(tmp_path / "empty.txt")
    assert view.text == "" and not view.has_more


def test_missing_file_raises_for_the_caller(tmp_path):
    with pytest.raises(FileNotFoundError):
        TextView().load_path(tmp_path / "gone.txt")


def test_loading_again_replaces_the_text(fixture_file):
    view = TextView()
    view.load_path(fixture_file("utf8.csv"))
    view.load_path(fixture_file("plain.txt"))
    assert view.text == fixture_file("plain.txt").read_text(encoding="utf-8")


# The widget


def test_read_only_monospace():
    view = TextView()
    assert not view.view.get_editable()
    assert view.view.get_monospace()
    # line numbers where GtkSourceView no longer leaks a view that shows them (5.12.1+)
    assert view.has_line_numbers == (text_view.GtkSource is not None and text_view._line_numbers_are_safe())


def test_note_is_shown_and_follows_the_language():
    view = TextView()
    assert view.note is None and not view._note_bar.get_revealed()
    view.show_note(SHOWN_AS_TEXT)
    assert view.note == "Shown as text, unknown format" and view._note_bar.get_revealed()
    assert not view._note_button.get_visible()
    i18n.set_language("ru")
    assert view.note == "Показано как текст: формат неизвестен"
    assert view._more_button.get_label() == "Показать ещё"
    view.hide_note()
    assert view.note is None and not view._note_bar.get_revealed()


def test_note_with_an_action():
    pressed = []
    view = TextView()
    view.show_note(N_("This format is not supported"), N_("View as Text"), lambda: pressed.append(1))
    assert view._note_button.get_visible() and view._note_button.get_label() == "View as Text"
    view._note_button.emit("clicked")
    assert pressed == [1]


def test_closed_view_is_freed_with_its_text(fixture_file):
    # Handlers kept from C must not hold the view, or every closed file's text would stay in memory
    window = Gtk.Window()
    view = TextView()
    view.load_path(fixture_file("plain.txt"))
    window.set_child(view)
    window.present()
    context = GLib.MainContext.default()
    end = time.monotonic() + 0.2
    while time.monotonic() < end:
        context.iteration(False)
    gone = weakref.ref(view)
    del view
    window.set_child(None)
    window.destroy()
    del window
    for _ in range(50):
        context.iteration(False)
    gc.collect()
    assert gone() is None
    assert not [r for r in i18n._listeners if r() is not None]
