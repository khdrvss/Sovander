"""Printing what is shown (Requirement 12.5), checked by printing into PDF files."""

import os
import time

import pytest
from gi.repository import Gtk

from sovander import i18n
from sovander import print as printing
from sovander.print import NotPrintable, print_view

pytestmark = pytest.mark.gui

try:
    import pymupdf as fitz
except ImportError:
    import fitz

SIZES = [(595, 842), (420, 298), (612, 792)]  # A4 portrait, A5 landscape, US Letter


@pytest.fixture(autouse=True)
def english():
    i18n.set_language("en")
    yield
    i18n.set_language("en")


def mixed_pdf() -> bytes:
    doc = fitz.open()
    for i, (w, h) in enumerate(SIZES):
        page = doc.new_page(width=w, height=h)
        page.insert_text((40, 60), f"Page {i + 1}", fontsize=24)
        page.draw_rect(fitz.Rect(40, 100, 140, 160), color=None, fill=(0.8, 0.1, 0.1))
    return doc.tobytes()


@pytest.fixture
def page_view(viewer, wait):
    viewer.load(mixed_pdf())
    assert wait(lambda: viewer.page_count == 3)
    return viewer


def pixel(page, x, y):
    pix = page.get_pixmap(alpha=False)
    return pix.pixel(int(x), int(y))


# Page view


def test_each_page_prints_on_paper_of_its_own_size(page_view, tmp_path):
    out = tmp_path / "out.pdf"
    page_view.night = True  # paper gets the document's own colours
    print_view(None, page_view, export_to=out)
    printed = fitz.open(out)
    assert printed.page_count == 3
    for page, (w, h) in zip(printed, SIZES, strict=True):
        assert abs(page.rect.width - w) < 1 and abs(page.rect.height - h) < 1
    r, g, b = pixel(printed[1], 90, 130)
    assert r > 150 and g < 80 and b < 80  # the red box, where it was, not inverted
    r, g, b = pixel(printed[1], 300, 250)
    assert min(r, g, b) > 230  # white paper


def test_standard_sizes_print_under_their_paper_names():
    assert printing._paper(595, 842).get_name() == Gtk.PAPER_NAME_A4
    assert printing._paper(842, 595).get_name() == Gtk.PAPER_NAME_A4
    assert printing._paper(612, 792).get_name() == Gtk.PAPER_NAME_LETTER
    odd = printing._paper(500, 300)
    assert odd.is_custom() and abs(odd.get_width(Gtk.Unit.POINTS) - 300) < 0.5


def test_each_page_is_drawn_once_when_gtk_asks_for_it(page_view, tmp_path, monkeypatch):
    # GTK picks the pages: it asks for only those in the user's range. No printer-free run can show
    # that: export always prints every page, and printing without the dialog never picks "Print to
    # File" (GTK skips virtual printers there) but the system's default printer. Sovander's part is
    # to draw each page GTK asks for, once and nothing more.
    drawn = []
    real = printing.render_page
    monkeypatch.setattr(printing, "render_page", lambda v, nr, dpi: drawn.append(nr) or real(v, nr, dpi))
    print_view(None, page_view, export_to=tmp_path / "out.pdf")
    assert drawn == [0, 1, 2]


def test_the_shown_page_is_offered_as_the_current_page(page_view, tmp_path):
    page_view.go_to(2)
    op = print_view(None, page_view, export_to=tmp_path / "out.pdf")
    assert op.get_property("current-page") == page_view.current_page


def test_pages_print_at_the_printer_resolution_within_limits(page_view):
    pixbuf, w, h = printing.render_page(page_view, 0, 300)
    assert (w, h) == SIZES[0]
    assert abs(pixbuf.get_width() - 595 * 300 / 72) < 2
    low, _w, _h = printing.render_page(page_view, 0, 72)  # export's 72 dpi is raised to 150
    assert abs(low.get_width() - 595 * 150 / 72) < 2
    assert printing.render_page(page_view, 9, 300) is None


# Text, image and web views


def test_text_is_wrapped_and_paginated(window, tmp_path, wait):
    from sovander.views.text_view import TextView

    view = TextView()
    window.set_child(view)
    lines = [f"line {n:03d} " + "word " * (40 if n == 5 else 3) for n in range(1, 301)]
    view.load_bytes("\n".join(lines).encode())
    out = tmp_path / "text.pdf"
    print_view(None, view, export_to=out)
    printed = fitz.open(out)
    assert printed.page_count > 3
    words = "".join(page.get_text() for page in printed)
    assert words.index("line 001") < words.index("line 150") < words.index("line 300")
    assert "line 001" in printed[0].get_text() and "line 300" in printed[-1].get_text()
    assert words.count("word") == 299 * 3 + 40  # the long line wrapped, nothing cut off


def test_empty_text_prints_one_blank_page(window, tmp_path):
    from sovander.views.text_view import TextView

    view = TextView()
    view.load_bytes(b"")
    print_view(None, view, export_to=tmp_path / "empty.pdf")
    assert fitz.open(tmp_path / "empty.pdf").page_count == 1


def test_an_image_prints_on_one_page(window, tmp_path, fixture_file):
    from sovander.views.image_view import ImageView

    view = ImageView()
    window.set_child(view)
    view.load_path(fixture_file("photo.webp"))
    out = tmp_path / "image.pdf"
    print_view(None, view, export_to=out)
    printed = fitz.open(out)
    assert printed.page_count == 1
    assert printed[0].rect.width > printed[0].rect.height  # a wide photo turns the paper


def test_a_book_or_markdown_prints_through_webkit(window, tmp_path, wait):
    from sovander.views.web_view import WebView

    view = WebView()
    window.set_child(view)
    window.present()
    view.load_markdown("# Printed heading\n\nSome **bold** words.\n", "notes.md")
    assert wait(lambda: view.is_ready, 20)
    out = tmp_path / "web.pdf"
    finished = []
    op = print_view(None, view, export_to=out)
    op.connect("finished", lambda *_args: finished.append(True))
    op.connect("failed", lambda _op, error: finished.append(error.message))
    end = time.monotonic() + 20
    assert wait(lambda: finished and os.path.exists(out) or time.monotonic() > end, 21)
    assert finished == [True]
    with fitz.open(out) as doc:  # PyMuPDF < 1.24 orphans a page of a collected document
        assert "Printed heading" in doc[0].get_text()


# What cannot be printed


def test_other_views_cannot_be_printed():
    with pytest.raises(NotPrintable, match="This document cannot be printed"):
        print_view(None, Gtk.Label())


def test_an_empty_image_view_cannot_be_printed():
    from sovander.views.image_view import ImageView

    with pytest.raises(NotPrintable):
        print_view(None, ImageView())


def test_an_empty_page_view_cannot_be_printed(viewer):
    with pytest.raises(NotPrintable):
        print_view(None, viewer)
