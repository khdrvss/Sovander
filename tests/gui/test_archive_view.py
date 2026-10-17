"""Archive browser: a zip like a folder, entries opened from memory, passwords, name encodings (8)."""

import builtins
import gc
import os
import weakref
import zipfile

import pytest
from gi.repository import GLib, Gtk

from sovander import archive, i18n
from sovander.archive import Archive
from sovander.views.archive_view import ArchiveView, encoding_label
from sovander.zipnames import CHOICES

pytestmark = pytest.mark.gui


@pytest.fixture(autouse=True)
def english():
    i18n.set_language("en")
    yield
    i18n.set_language("en")


@pytest.fixture
def browse(window, wait, fixture_file):
    """An ArchiveView of a fixture zip in a window; ``view.got`` collects what it hands on."""

    def make(name, **kwargs):
        got = []
        view = ArchiveView(Archive(fixture_file(name)), lambda entry, data: got.append((entry, data)), name,
                           **kwargs)
        view.got = got
        window.set_child(view)
        window.present()
        assert wait(lambda: view.get_mapped())
        return view

    return make


def names(view):
    return [e.name for e in view.shown()]


def entry(view, name):
    return next(e for e in view.shown() if e.name == name)


def open_and_wait(view, wait, name, password=None):
    assert view.open_entry(entry(view, name), password)
    assert wait(lambda: not view.busy)


def crumbs(view):
    labels, child = [], view._crumbs.get_first_child()
    while child is not None:
        labels.append(child.get_label())
        child = child.get_next_sibling()
    return labels


# Listing (8.1)


def test_folders_first_then_files_and_no_macos_junk(browse):
    view = browse("archive.zip")
    assert names(view) == ["nested", "photos", "private", "reports", "plain.txt"]
    assert view.folder == "" and crumbs(view) == ["archive.zip"]
    assert not view._actions.lookup_action("up").get_enabled()


def test_folders_open_in_place_and_up_goes_back(browse, wait):
    view = browse("archive.zip")
    open_and_wait(view, wait, "photos")
    assert view.folder == "photos" and names(view) == [".hidden-thumbs", "tiny.png"]
    assert crumbs(view) == ["archive.zip", "photos"]
    assert view._actions.lookup_action("up").get_enabled()
    view.activate_action("archive.up", None)
    assert view.folder == ""


def test_the_path_bar_jumps_to_a_folder_above(browse, fixture_file):
    view = browse("archive.zip")
    view.show_folder("reports")
    view._crumbs.get_first_child().emit("clicked")
    assert view.folder == ""


def test_rows_show_sizes(browse):
    view = browse("archive.zip")
    plain = entry(view, "plain.txt")
    assert plain.size == 119 and GLib.format_size(plain.size) == "119 bytes"


# Opening entries (8.2, 8.3)


def test_a_file_is_read_in_memory_and_handed_on(browse, wait, fixture_file, monkeypatch):
    view = browse("archive.zip")

    def refuse(*args, **kwargs):
        raise AssertionError(f"tried to open {args[0]!r}")

    monkeypatch.setattr(builtins, "open", refuse)
    monkeypatch.setattr(os, "open", refuse)
    open_and_wait(view, wait, "plain.txt")
    monkeypatch.undo()
    assert view.got == [("plain.txt", fixture_file("plain.txt").read_bytes())]


def test_a_zip_inside_hands_on_its_bytes(browse, wait):
    view = browse("archive.zip")
    view.show_folder("nested")
    open_and_wait(view, wait, "stored.zip")
    name, data = view.got[0]
    assert name == "stored.zip" and data.startswith(b"PK\x03\x04")


# Passwords (8.4)


def test_a_locked_entry_asks_says_when_wrong_and_asks_again(browse, wait, fixture_file):
    view = browse("locked.zip")
    open_and_wait(view, wait, "zipcrypto.txt")
    dialog = view.password_dialog
    assert dialog is not None and dialog.get_modal() and not dialog.error.get_visible()
    dialog.entry.set_text("goose")
    dialog.accept()
    assert wait(lambda: not view.busy and view.password_dialog not in (None, dialog))
    again = view.password_dialog
    assert again.error.get_visible() and view.got == []
    again.entry.set_text("gander")
    again.entry.emit("activate")  # Enter in the field
    assert wait(lambda: view.got)
    assert view.got == [("zipcrypto.txt", fixture_file("plain.txt").read_bytes())]


def test_the_password_that_worked_is_tried_first_next_time(browse, wait, fixture_file):
    view = browse("locked.zip")
    open_and_wait(view, wait, "aes128.txt", "gander")
    open_and_wait(view, wait, "aes192.pdf")
    assert view.password_dialog is None
    assert view.got[-1] == ("aes192.pdf", fixture_file("six-pages.pdf").read_bytes())


def test_cancel_and_escape_leave_the_entry_closed(browse, wait):
    view = browse("locked.zip")
    open_and_wait(view, wait, "zipcrypto.png")
    view.password_dialog.cancel()
    assert view.password_dialog is None and view.got == []
    open_and_wait(view, wait, "zipcrypto.png")
    dialog = view.password_dialog
    dialog.activate_action("window.close", None)
    assert wait(lambda: view.password_dialog is None)
    assert view.got == [] and not view.busy


# What cannot be read (8.4, 10.4)


def test_an_unsupported_method_says_so(browse, wait):
    view = browse("deflate64.zip")
    open_and_wait(view, wait, "long.txt")
    assert "not supported" in view.message and view.got == []


def test_an_unsupported_encryption_says_so_without_asking(browse, wait):
    view = browse("locked.zip")
    open_and_wait(view, wait, "aes256-deflate64.md")
    assert view.password_dialog is None and "not supported" in view.message


def test_an_entry_over_the_cap_is_too_large(browse, wait, monkeypatch):
    view = browse("archive.zip")
    monkeypatch.setattr(archive, "MAX_ENTRY_BYTES", 100)
    open_and_wait(view, wait, "plain.txt")
    assert view.message == "Too large to open safely" and view.got == []


def test_a_damaged_entry_says_so(window, wait, tmp_path):
    path = tmp_path / "bad.zip"
    with zipfile.ZipFile(path, "w") as z:
        z.writestr("note.txt", "hello, damaged world")
    data = bytearray(path.read_bytes())
    data[data.index(b"hello")] ^= 0xFF  # stored bytes no longer match their checksum
    path.write_bytes(bytes(data))
    got = []
    view = ArchiveView(Archive(path), lambda name, data: got.append(name))
    window.set_child(view)
    open_and_wait(view, wait, "note.txt")
    assert view.message == "This file is damaged" and got == []
    view._on_dismiss()
    assert view.message is None


# Name encodings (8.5, 8.6)


def test_names_are_read_again_in_the_encoding_picked(browse):
    view = browse("names-cp866.zip")
    assert view._encoding.get_sensitive()
    assert names(view) == ["Документы", "Фото"]
    view.activate_action("archive.encoding", GLib.Variant.new_string("cp1251"))
    assert view.archive.name_encoding == "cp1251"
    assert view._actions.lookup_action("encoding").get_state().get_string() == "cp1251"
    assert names(view) != ["Документы", "Фото"]
    view.activate_action("archive.encoding", GLib.Variant.new_string(""))
    assert names(view) == ["Документы", "Фото"]


def test_an_open_folder_survives_a_change_back(browse):
    view = browse("names-cp866.zip")
    view.show_folder("Фото")
    view.set_name_encoding("cp1251")
    assert view.folder == ""  # the folder's name reads differently now
    view.set_name_encoding("cp866")
    view.show_folder("Фото")
    view.set_name_encoding("cp866")
    assert view.folder == "Фото"


def test_the_menu_lists_every_choice_with_a_name(browse):
    view = browse("names-cp866.zip")
    menu = view._encoding.get_menu_model()
    choices = menu.get_item_link(1, "section")
    assert choices.get_n_items() == len(CHOICES)
    assert encoding_label("cp866") == "Cyrillic, DOS (CP866)"
    assert encoding_label("x-unknown") == "X-UNKNOWN"


def test_nothing_to_choose_when_every_name_says_its_encoding(browse):
    assert not browse("archive.zip")._encoding.get_sensitive()


def test_labels_follow_the_language(browse):
    view = browse("names-cp866.zip")
    i18n.set_language("ru")
    assert view._columns[0].get_title() == "Имя"
    assert view._encoding.get_label() == "Кодировка имён"
    assert encoding_label("cp866") == "Кириллица, DOS (CP866)"


# Lifetime


def test_a_closed_archive_view_is_freed_and_closes_its_archive(wait, fixture_file):
    window = Gtk.Window()
    zipped = Archive(fixture_file("locked.zip"))
    view = ArchiveView(zipped, lambda name, data: None)
    window.set_child(view)
    window.present()
    assert wait(view.get_mapped)
    open_and_wait(view, wait, "open.txt")
    open_and_wait(view, wait, "zipcrypto.txt")  # leaves a password dialog open, holding the view weakly
    dialog = view.password_dialog
    gone = weakref.ref(view)
    del view
    window.destroy()
    del window
    for _ in range(3):
        wait(lambda: False, 0.1)
        gc.collect()
    assert gone() is None
    assert zipped._fd is None
    dialog.accept()  # answering after the view went is harmless
    dialog.destroy()
