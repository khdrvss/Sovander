"""Cell text in rows shorter than a line of text (found in an end-to-end run: an .ods at the usual 17 px)."""

import pytest

from sovander.engine.sheets import Cell
from sovander.views.sheet_view import SheetView
from tests.gui.test_sheet_view import Book, grid, pump, show

pytestmark = pytest.mark.gui


@pytest.fixture
def view(window):
    widget = SheetView()
    window.set_child(widget)
    window.present()
    assert pump(lambda: widget.area.get_width() > 0)
    return widget


def ink_rows(image, x, y, w, h) -> int:
    """How many pixel rows inside the cell hold text (any pixel much darker than white)."""
    return sum(any(sum(image.pixel(px, py)[:3]) < 450 for px in range(int(x) + 2, int(x + w) - 2))
               for py in range(int(y) + 1, int(y + h) - 1))


def test_text_in_a_short_row_keeps_its_descenders(view, pixels):
    """Spreadsheets bottom-align text: a row shorter than the line loses empty ascent, not the
    descenders of g, y and p (with the text pinned to the top, their tails were cut off)."""
    show(view, Book(grid(rows=2, cols=1, cells={(0, 0): Cell("gyp"), (1, 0): Cell("gyp")},
                         heights=[15, 60], widths=[80])))
    image = pixels(view.area)
    short, tall = ink_rows(image, *view.area.cell_rect(0, 0)), ink_rows(image, *view.area.cell_rect(1, 0))
    assert tall >= 8 and short >= tall - 1, (short, tall)
