"""The sheet view over workbooks read by the real engine (Requirements 4.1-4.5).

Run with ``xvfb-run -a scripts/dev-env.sh python3 -m pytest -m "engine and gui" tests/engine``.
"""

import dataclasses
import functools
import time

import pytest

try:
    import pymupdf as fitz
except ImportError:
    import fitz

gi = pytest.importorskip("gi")
gi.require_version("Gdk", "4.0")
gi.require_version("Graphene", "1.0")
gi.require_version("Gtk", "4.0")
from gi.repository import Gdk, GLib, Graphene, Gtk  # noqa: E402

from sovander import i18n  # noqa: E402
from sovander.engine import sheets  # noqa: E402
from sovander.views.sheet_view import SheetView  # noqa: E402

pytestmark = [pytest.mark.engine, pytest.mark.gui]

FOOTER = "xlsx/45540_classic_Footer.xlsx"  # yellow A4 on its sheets, stored formulas in column B


def pump(until, timeout=10.0) -> bool:
    context = GLib.MainContext.default()
    end = time.monotonic() + timeout
    while not until():
        if time.monotonic() > end:
            return False
        if not context.iteration(False):
            time.sleep(0.002)
    return True


@pytest.fixture
def view():
    if Gdk.Display.get_default() is None:
        pytest.skip("no display: run under xvfb-run -a")
    i18n.set_language("en")
    window = Gtk.Window(default_width=900, default_height=600)
    widget = SheetView()
    window.set_child(widget)
    window.present()
    assert pump(lambda: widget.area.get_width() > 0)
    yield widget
    window.destroy()
    pump(lambda: False, 0.05)


def show(view, book, **kwargs):
    view.load(book, **kwargs)
    assert pump(lambda: not view.loading)


def pixel(widget, x, y):
    if gi.version_info < (3, 44):
        # As the GUI pixels fixture: PyGObject 3.42 (Ubuntu 22.04) cannot hand a GskRenderNode back to GTK
        # and crashes freeing it in a later collection; skip before making one
        pytest.skip("pixel checks need PyGObject 3.44 or newer (Ubuntu 24.04)")
    snapshot = Gtk.Snapshot()
    widget.do_snapshot(snapshot)
    bounds = Graphene.Rect().init(0, 0, widget.get_width(), widget.get_height())
    texture = widget.get_native().get_renderer().render_texture(snapshot.to_node(), bounds)
    return fitz.Pixmap(texture.save_to_png_bytes().get_data()).pixel(int(x), int(y))[:3]


class WithoutFormulas:
    """A workbook whose grids lost their formulas, so the view must ask the engine."""

    def __init__(self, book):
        self.book, self.sheets = book, book.sheets

    def grid(self, index):
        return dataclasses.replace(self.book.grid(index), formulas={})


def test_tabs_values_and_find_in_an_ods(engine, fixture_file, view):
    show(view, sheets.workbook(fixture_file("budget.ods"), engine=engine))
    assert view.sheet_names == ["Summary", "Detail", "Notes"]
    assert [b.get_label() for b in view._tab_buttons] == view.sheet_names
    assert view.select_cell(1, 2) and view.address == "C2" and view.formula_text == "4200"
    assert view.find("detail-sheet") == (0, 1)
    assert (view.sheet_index, view.selected_cell, view.address) == (1, (1, 1), "B2")


def test_fills_and_stored_formulas_of_an_xlsx(engine, corpus_file, view):
    book = sheets.workbook(corpus_file(FOOTER), engine=engine)
    show(view, book)
    view.show_sheet(1)
    assert pump(lambda: not view.loading and view.sheet_index == 1)
    grid = book.grid(1)
    assert grid.cells[(3, 0)].fill == "#FFFF00"
    x, y, w, h = view.area.cell_rect(3, 0)
    assert pixel(view.area, x + w - 8, y + h / 2) == (255, 255, 0)  # right of the text, in A4's fill
    row, col = next(iter(grid.formulas))
    assert view.select_cell(row, col)
    assert view.formula_text == grid.formulas[(row, col)] and view.formula_text.startswith("=")
    assert view.formula_text != grid.cells[(row, col)].text  # the stored value is what the cell shows


def test_formula_fetched_from_the_engine_on_demand(engine, corpus_file, view):
    path = corpus_file(FOOTER)
    book = sheets.workbook(path, engine=engine)
    expected = book.grid(1).formulas[(8, 1)]
    show(view, WithoutFormulas(book), formula=functools.partial(sheets.formula, path, engine=engine))
    view.show_sheet(1)
    assert pump(lambda: not view.loading and view.sheet_index == 1)
    assert view.select_cell(8, 1) and view.address == "B9"
    assert view.formula_text == book.grid(1).cells[(8, 1)].text  # the value until the engine answers
    assert pump(lambda: view.formula_text == expected, timeout=60)
