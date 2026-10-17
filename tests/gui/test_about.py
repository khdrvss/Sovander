"""About: version, licence texts in full, credits, offline status and AppArmor label (9.7, NFR Licensing)."""

import pytest
from gi.repository import Gtk

from sovander import __version__, about, i18n, offline
from sovander.about import about_info, licence_text, offline_lines, show_about
from sovander.offline import OfflineStatus

pytestmark = pytest.mark.gui


@pytest.fixture(autouse=True)
def english():
    i18n.set_language("en")
    yield
    i18n.set_language("en")


@pytest.fixture
def confined(monkeypatch):
    monkeypatch.setattr(offline, "confinement", lambda: "sovander (enforce)")


def test_the_offline_status_and_profile_are_shown(confined):
    info = about_info(OfflineStatus.VERIFIED)
    assert "Offline: verified by the system" in info.comments
    assert "AppArmor profile: sovander (enforce)" in info.comments
    assert "Offline: verified by the system" in info.system


def test_a_warning_when_the_network_is_not_blocked(monkeypatch):
    monkeypatch.setattr(offline, "confinement", lambda: None)
    info = about_info(OfflineStatus.UNPROTECTED)
    assert "Offline protection is off: the network is not blocked" in info.comments
    assert "AppArmor: not available" in info.comments


def test_the_status_is_checked_when_not_given(monkeypatch):
    calls = []
    monkeypatch.setattr(offline, "check", lambda: calls.append(1) or OfflineStatus.UNKNOWN)
    assert "could not be checked" in about_info().comments and calls == [1]


def test_every_licence_text_is_there_in_full():
    text = licence_text()
    assert "GNU AFFERO GENERAL PUBLIC LICENSE" in text and "END OF TERMS AND CONDITIONS" in text
    assert "Permission is hereby granted, free of charge" in text  # MIT: Gander, marked, JSZip
    assert "Apache License" in text and "Grant of Patent License" in text  # DOMPurify
    for component in ("Gander", "marked", "DOMPurify", "JSZip", "pako"):
        assert component in text


def test_a_missing_licence_file_is_named(tmp_path):
    text = licence_text((tmp_path / "copyright",), tmp_path / "LICENSES.md")
    assert "missing from this installation" in text and str(tmp_path / "LICENSES.md") in text


def test_credits_name_every_engine_library_and_font():
    people = " ".join(name for _title, names in about_info(OfflineStatus.VERIFIED).credits for name in names)
    for part in ("Gander", "MIT", "LibreOffice", "MuPDF", "PyMuPDF", "AGPL-3.0", "WebKitGTK", "marked",
                 "DOMPurify", "JSZip", "Carlito", "Caladea"):
        assert part in people, part


def test_the_dialog(window, confined, wait):
    dialog = show_about(window, OfflineStatus.VERIFIED)
    try:
        assert isinstance(dialog, Gtk.AboutDialog) and dialog.get_transient_for() is window
        assert dialog.get_version() == __version__ and dialog.get_program_name() == "Sovander"
        assert dialog.get_license_type() == Gtk.License.CUSTOM
        assert "GNU AFFERO GENERAL PUBLIC LICENSE" in dialog.get_license()
        assert "AppArmor profile: sovander (enforce)" in dialog.get_comments()
        # the project page, since the public release (public-release D8): opened only after the
        # confirmation every link gets, by the browser, outside the offline app
        assert dialog.get_website() == "https://github.com/khdrvss/Sovander"
        assert wait(lambda: dialog.get_mapped())
    finally:
        dialog.destroy()


def test_a_link_in_the_dialog_asks_first(window, monkeypatch):
    asked = []
    monkeypatch.setattr(about.links, "confirm_and_open", lambda parent, uri: asked.append((parent, uri)))
    dialog = show_about(window, OfflineStatus.VERIFIED)
    try:
        assert dialog.emit("activate-link", "https://www.gnu.org/licenses/") is True
        assert asked == [(dialog, "https://www.gnu.org/licenses/")]
    finally:
        dialog.destroy()


def test_in_russian(confined):
    i18n.set_language("ru")
    info = about_info(OfflineStatus.VERIFIED)
    assert offline.message(OfflineStatus.VERIFIED) in info.comments
    assert "Профиль AppArmor: sovander (enforce)" in info.comments
    assert info.credits[0][0] == "Основано на"
    assert offline_lines(OfflineStatus.VERIFIED, None).endswith("AppArmor: недоступен")


def test_the_project_page_is_offered_through_the_link_confirmation(monkeypatch):
    """The public page is in About (public-release R6), and like every link it is asked about first."""
    from sovander import about

    asked = []
    monkeypatch.setattr(about.links, "confirm_and_open", lambda parent, uri: asked.append(uri))
    dialog = about.show_about(None)
    try:
        assert dialog.get_website() == about.HOMEPAGE == "https://github.com/khdrvss/Sovander"
        assert about._on_link(dialog, about.HOMEPAGE) is True and asked == [about.HOMEPAGE]
    finally:
        dialog.destroy()
