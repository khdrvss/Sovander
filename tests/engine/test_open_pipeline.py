"""The open pipeline over the real engine (task 5.1; Requirements 2.2-2.5, 3.8-3.10, 5.7, 10.2).

Run with ``xvfb-run -a scripts/dev-env.sh python3 -m pytest -m engine tests/engine/test_open_pipeline.py``.
"""

import time
import zipfile
from pathlib import Path

import pytest

gi = pytest.importorskip("gi")
gi.require_version("Gdk", "4.0")
gi.require_version("Gtk", "4.0")
from gi.repository import Gdk, GLib, Gtk  # noqa: E402

from sovander import i18n  # noqa: E402
from sovander.engine import client  # noqa: E402
from sovander.open_document import Context, DocumentPage, Source  # noqa: E402
from sovander.store import Store  # noqa: E402
from sovander.views.page_view import PageView  # noqa: E402
from sovander.views.sheet_view import SheetView  # noqa: E402

pytestmark = [pytest.mark.engine, pytest.mark.gui]

FIXTURES = Path(__file__).resolve().parents[1] / "fixtures"
PHRASE = "sovander-pipeline-phrase"  # throwaway key for the file encrypted below


def pump(until, timeout=60.0) -> bool:
    context = GLib.MainContext.default()
    end = time.monotonic() + timeout
    while not until():
        if time.monotonic() > end:
            return False
        if not context.iteration(False):
            time.sleep(0.002)
    return True


@pytest.fixture(autouse=True)
def english():
    i18n.set_language("en")


@pytest.fixture
def context(engine, tmp_path):
    return Context(Store(tmp_path / "data"), engine, tmp_path / "renders")


@pytest.fixture
def show():
    """show(source, context) -> a started DocumentPage in a window."""
    if Gdk.Display.get_default() is None:
        pytest.skip("no display: run under xvfb-run -a")
    windows = []

    def make(source, context):
        page = DocumentPage(source, context)
        window = Gtk.Window(default_width=800, default_height=600, child=page)
        window.present()
        windows.append(window)
        page.start()
        return page

    yield make
    for window in windows:
        window.destroy()
    pump(lambda: False, 0.05)


def record(page):
    """(page count, loading row) as the page view goes through them."""
    seen = []

    def watch(*_args):
        view = page.view
        if isinstance(view, PageView):
            for prop in ("page-count", "loading-more"):
                view.connect(f"notify::{prop}", lambda v, _p: seen.append((v.page_count, v.loading_more)))

    page.connect("view-changed", watch)
    return seen


def test_word_file_shows_page_one_then_all_pages(engine, context, show):
    started = time.monotonic()
    page = show(Source.from_path(FIXTURES / "word-pages.docx"), context)
    seen = record(page)
    assert pump(lambda: isinstance(page.view, PageView) and page.view.page_count >= 1)
    first = time.monotonic() - started
    assert pump(lambda: not page.view.loading_more and page.view.page_count > 1)
    assert seen[0][0] == 1 and (1, True) in seen, seen
    assert seen[-1] == (page.view.page_count, False) and page.view.page_count > 1  # LibreOffice: 3 pages
    print(f"\nwarm engine, word-pages.docx: page 1 after {first:.2f} s")


def test_second_open_is_served_from_the_cache_without_the_engine(engine, context, show, monkeypatch):
    first = show(Source.from_path(FIXTURES / "word-pages.docx"), context)
    assert pump(lambda: isinstance(first.view, PageView) and first.view.page_count > 1
                and not first.view.loading_more)
    jobs = []
    run = engine.run
    monkeypatch.setattr(engine, "run", lambda job, timeout=None: jobs.append(job) or run(job, timeout))
    again = show(Source.from_path(FIXTURES / "word-pages.docx"), context)
    assert pump(lambda: isinstance(again.view, PageView), 5)
    assert (again.view.page_count, again.view.loading_more, jobs) == (first.view.page_count, False, [])


@pytest.fixture(scope="module")
def encrypted_odt(engine):
    def make(eng):
        doc, _ = client.open_document(eng, (FIXTURES / "letter.odt").read_bytes(), client.TEXT)
        try:
            out = eng.create("com.sun.star.io.SequenceOutputStream")
            args = {"FilterName": "writer8", "OutputStream": out, "Password": PHRASE}
            doc.storeToURL("private:stream", client.props(**args))
            return out.getWrittenBytes().value
        finally:
            client.close_document(doc)

    return engine.run(make)


def test_password_document_asks_says_wrong_then_opens(context, show, encrypted_odt):
    page = show(Source.from_bytes("locked.odt", encrypted_odt), context)
    assert pump(lambda: page.state == "password")
    assert not page.wrong_password
    page.password_entry.set_text(PHRASE + "-typo")
    page.submit_password()
    assert pump(lambda: page.state == "password" and page.wrong_password)
    page.password_entry.set_text(PHRASE)
    page.submit_password()
    assert pump(lambda: isinstance(page.view, PageView) and not page.view.loading_more)
    assert page.view.page_count >= 1
    assert not list((context._cache_root).rglob("*.pdf"))  # a decrypted render is never kept


def test_password_word_file_from_the_corpus_asks(context, show, corpus_file):
    page = show(Source.from_path(corpus_file("docx/bug53475-password-is-solrcell.docx")), context)
    assert pump(lambda: page.state in ("password", "error", "ready"))
    assert page.state == "password", page.error_text


def test_spreadsheet_opens_in_the_sheet_view(context, show):
    page = show(Source.from_path(FIXTURES / "budget.xlsx"), context)
    # the first grid is parsed off the main thread after load
    assert pump(lambda: page.state == "ready" and not page.view.loading)
    assert isinstance(page.view, SheetView) and page.view.sheet_names


def test_damaged_word_file_shows_the_readable_part(context, show, repairs_ooxml):
    data = (FIXTURES / "report.docx").read_bytes()
    page = show(Source.from_bytes("cut.docx", data[: int(len(data) * 0.9)]), context)
    assert pump(lambda: page.state in ("ready", "error") and not getattr(page.view, "loading_more", False))
    assert isinstance(page.view, PageView), page.error_text
    assert "This file is damaged. Showing the readable part." in page.notes


def test_macros_are_noted_and_the_document_still_shows(context, show, tmp_path):
    docm = tmp_path / "macro.docm"
    with zipfile.ZipFile(FIXTURES / "report.docx") as source, zipfile.ZipFile(docm, "w") as out:
        for item in source.infolist():
            out.writestr(item, source.read(item))
        out.writestr("word/vbaProject.bin", b"\xd0\xcf\x11\xe0 never run")
    page = show(Source.from_path(docm), context)
    assert pump(lambda: page.state in ("ready", "error"))
    assert isinstance(page.view, PageView), page.error_text
    assert "This document contains macros. They were not run." in page.notes
