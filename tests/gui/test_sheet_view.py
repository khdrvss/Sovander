import gc
import threading
import time
import weakref
from collections.abc import Mapping

import pytest

gi = pytest.importorskip("gi")
gi.require_version("Gdk", "4.0")
gi.require_version("Graphene", "1.0")
gi.require_version("Gtk", "4.0")
gi.require_version("Adw", "1")
from gi.repository import Adw, Gdk, GLib, Graphene, Gtk  # noqa: E402

from sovander import i18n  # noqa: E402
from sovander.engine.sheets import Cell, Grid  # noqa: E402
from sovander.views.sheet_view import SheetView, cell_address, column_letters, looks_numeric  # noqa: E402

pytestmark = pytest.mark.gui

YELLOW, BLUE, RED = "#FFFF00", "#0000FF", "#FF0000"


class Book:
    """The part of sheets.Workbook the view uses; ``gate`` holds grid() back until it is set."""

    def __init__(self, *grids, gate=None):
        self.grids, self.gate, self.asked = list(grids), gate, []
        self.sheets = [g.name if g else f"Broken{k}" for k, g in enumerate(grids)]

    def grid(self, index):
        self.asked.append(index)
        if self.gate is not None:
            self.gate.wait(10)
        if self.grids[index] is None:
            raise ValueError("broken sheet")
        return self.grids[index]


def grid(name="Sheet1", rows=4, cols=4, cells=None, heights=None, widths=None, formulas=None, row_ids=None,
         col_ids=None):
    row_ids = row_ids or list(range(rows))
    col_ids = col_ids or list(range(cols))
    return Grid(name, row_ids, col_ids, heights or [20] * len(row_ids), widths or [60] * len(col_ids),
                cells or {}, formulas or {})


def pump(until, timeout=10.0) -> bool:
    """Run the GLib main loop until ``until()`` is true or ``timeout`` seconds pass."""
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
    yield
    i18n.set_language("en")


@pytest.fixture
def view(window):
    widget = SheetView()
    window.set_child(widget)
    window.present()
    assert pump(lambda: widget.area.get_width() > 0)
    return widget


def show(view, book, **kwargs):
    view.load(book, **kwargs)
    assert pump(lambda: not view.loading)
    pump(lambda: False, 0.05)  # let allocation and the merge index catch up


def colour(pixels, widget, x, y):
    return pixels(widget).pixel(int(x), int(y))[:3]


def near(a, b, tolerance=12):
    return all(abs(p - q) <= tolerance for p, q in zip(a, b, strict=True))


def centre(view, i, j):
    x, y, w, h = view.area.cell_rect(i, j)
    return x + w / 2, y + h / 2


def has_ink(image, x0, y0, x1, y1):
    """True if any pixel in the box is much darker than white (text was drawn there)."""
    box = ((x, y) for x in range(int(x0), int(x1)) for y in range(int(y0), int(y1)))
    return any(sum(image.pixel(x, y)[:3]) < 450 for x, y in box)


# Names and alignment


@pytest.mark.parametrize(("col", "letters"), [(0, "A"), (25, "Z"), (26, "AA"), (51, "AZ"), (52, "BA"),
                                              (701, "ZZ"), (702, "AAA"), (16383, "XFD")])
def test_column_letters(col, letters):
    assert column_letters(col) == letters


def test_cell_address():
    assert cell_address(0, 0) == "A1"
    assert cell_address(9, 27) == "AB10"


@pytest.mark.parametrize("text", ["1,234.50 EUR", "12%", "-3", "(1 234,56 ₽)", "$5.00", "2026-10-17",
                                  "17.10.2026", "1.5E+10", "12:30"])
def test_numbers_dates_and_money_align_right(text):
    assert looks_numeric(text)


@pytest.mark.parametrize("text", ["Q3 2026", "Total", "3 apples", "", "#N/A"])
def test_words_align_left(text):
    assert not looks_numeric(text)


# Tabs, headers, layout (4.1, 4.4)


def test_one_tab_per_sheet_and_switching(view):
    book = Book(grid("Jan"), grid("Feb"), grid("Mar", cells={(0, 0): Cell("march")}))
    show(view, book)
    assert view.sheet_names == ["Jan", "Feb", "Mar"]
    assert [b.get_label() for b in view._tab_buttons] == ["Jan", "Feb", "Mar"]
    assert view.sheet_index == 0 and view._tab_buttons[0].get_active()
    view._tab_buttons[2].set_active(True)
    assert pump(lambda: view.sheet_index == 2 and not view.loading)
    assert view.formula_text == "march"
    view.show_sheet(0)
    assert view._tab_buttons[0].get_active() and not view._tab_buttons[2].get_active()
    assert book.asked.count(0) == 1  # a sheet is parsed once
    with pytest.raises(IndexError):
        view.show_sheet(3)


def test_headers_show_letters_and_real_row_numbers(view):
    show(view, Book(grid(row_ids=[0, 1, 4, 9], col_ids=[0, 2, 27])))
    assert [view.area.row_label(i) for i in range(4)] == ["1", "2", "5", "10"]
    assert [view.area.column_label(j) for j in range(3)] == ["A", "C", "AB"]


def test_widths_heights_and_fills_as_saved(view, pixels):
    cells = {(1, 1): Cell("", YELLOW)}
    show(view, Book(grid(rows=3, cols=3, cells=cells, heights=[20, 40, 20], widths=[50, 120, 60])))
    x, y, w, h = view.area.cell_rect(1, 1)
    x0, y0, _w, _h = view.area.cell_rect(0, 0)
    assert (x - x0, y - y0, w, h) == (50, 20, 120, 40)
    assert x0 >= 30 and y0 >= 15  # frozen headers before the first cell
    assert colour(pixels, view.area, *centre(view, 1, 1)) == (255, 255, 0)
    assert colour(pixels, view.area, *centre(view, 2, 2)) == (255, 255, 255)


def test_dark_theme_changes_the_grid_and_keeps_fills(view, pixels, light_theme):
    show(view, Book(grid(cells={(1, 1): Cell("", YELLOW)})))
    light_theme.set_color_scheme(Adw.ColorScheme.FORCE_DARK)  # the conftest fixture turns it light again
    pump(lambda: False, 0.1)
    assert sum(colour(pixels, view.area, *centre(view, 2, 2))) < 150
    assert colour(pixels, view.area, *centre(view, 1, 1)) == (255, 255, 0)


def test_merged_cell_covers_its_span_without_inner_lines(view, pixels):
    cells = {(0, 0): Cell("", BLUE, rowspan=2, colspan=2), (3, 3): Cell("x")}
    show(view, Book(grid(rows=5, cols=5, cells=cells)))
    x, y, w, h = view.area.cell_rect(0, 0)
    assert (w, h) == (120, 40)
    image = pixels(view.area)
    assert image.pixel(int(x + 59), int(y + 30))[:3] == (0, 0, 255)  # where A's right line would be
    assert image.pixel(int(x + 30), int(y + 19))[:3] == (0, 0, 255)  # where row 1's bottom line would be
    view.select_cell(1, 1)  # a covered cell selects its merge
    assert view.selected_cell == (0, 0) and view.address == "A1"


def test_merge_is_drawn_when_its_top_left_scrolled_away(view, pixels):
    cells = {(0, 0): Cell("tall", RED, rowspan=50, colspan=1)}
    show(view, Book(grid(rows=200, cols=3, cells=cells)))
    assert pump(lambda: view.area._indexed)
    view.area.props.vadjustment.set_value(20 * 10)
    x, _y, w, _h = view.area.cell_rect(0, 0)
    assert colour(pixels, view.area, x + w / 2, view.area._hh + 30) == (255, 0, 0)
    view.select_cell(30, 0)
    assert view.selected_cell == (0, 0)


def test_long_text_runs_over_empty_cells_only(view, pixels):
    long = "WWWWWWWWWWWWWWWWWWWWWWWW"
    cells = {(0, 0): Cell(long), (1, 0): Cell(long), (1, 1): Cell("", "#FEFEFE"), (2, 2): Cell("123")}
    show(view, Book(grid(rows=3, cols=3, cells=cells, widths=[60, 60, 200])))
    image = pixels(view.area)
    x, y, w, h = view.area.cell_rect(0, 1)
    assert has_ink(image, x + 2, y + 2, x + w - 2, y + h - 2)
    x, y, w, h = view.area.cell_rect(1, 1)
    assert not has_ink(image, x + 2, y + 2, x + w - 2, y + h - 2)
    x, y, w, h = view.area.cell_rect(2, 2)  # numbers sit on the right
    assert has_ink(image, x + w / 2, y + 2, x + w - 2, y + h - 2)
    assert not has_ink(image, x + 2, y + 2, x + w / 2, y + h - 2)


# Selection and formulas (4.2, 4.5)


def test_selected_cell_shows_address_and_stored_formula_or_value(view):
    cells = {(0, 0): Cell("2"), (0, 1): Cell("999")}
    show(view, Book(grid(cells=cells, formulas={(0, 1): "=A1*10"})))
    assert view.selected_cell == (0, 0) and view.address == "A1" and view.formula_text == "2"
    assert view.select_cell(0, 1)
    assert view.address == "B1" and view.formula_text == "=A1*10"
    x, y = centre(view, 0, 1)
    assert view.area.at(x, y) == (0, 1)
    assert view.area.at(1, 1) is None  # the corner header
    assert view.select_cell(3, 3) and view.formula_text == ""


def test_formula_fetched_on_demand_for_the_last_cell_only(view):
    asked = []

    def source(sheet, row, col):
        asked.append((sheet, row, col))
        return f"=F{row}{col}" if (row, col) != (2, 2) else None

    show(view, Book(grid(rows=3, cols=3, row_ids=[0, 4, 7], cells={(4, 2): Cell("5")})), formula=source)
    view.select_cell(0, 1)
    view.select_cell(4, 1)
    view.select_cell(4, 2)
    assert view.formula_text == "5"  # the value until the formula arrives
    assert pump(lambda: view.formula_text == "=F42")
    assert asked == [("Sheet1", 4, 2)]


def test_late_formula_is_dropped(view):
    gate = threading.Event()

    def source(_sheet, _row, _col):
        gate.wait(5)
        return "=LATE()"

    show(view, Book(grid(cells={(1, 1): Cell("b2")})), formula=source)
    view.select_cell(0, 0)
    pump(lambda: False, 0.3)  # the fetch for A1 starts and waits
    view._formula_source = None
    view.select_cell(1, 1)
    gate.set()
    pump(lambda: False, 0.3)
    assert view.formula_text == "b2"


# Keyboard


def test_keyboard_navigation(view):
    cells = {(1, 1): Cell("m", rowspan=2, colspan=2)}
    show(view, Book(grid(rows=200, cols=10, cells=cells)))
    area = view.area
    area.handle_key(Gdk.KEY_Right)
    area.handle_key(Gdk.KEY_Down)
    assert view.selected_cell == (1, 1)  # into the merge
    area.handle_key(Gdk.KEY_Right)
    assert view.selected_cell == (1, 3)  # out past its span
    area.handle_key(Gdk.KEY_Left)
    assert view.selected_cell == (1, 1)
    area.handle_key(Gdk.KEY_Page_Down)
    rows_per_page = area.props.vadjustment.get_page_size() // 20
    assert view.selected_cell[0] >= rows_per_page - 1
    area.handle_key(Gdk.KEY_End, Gdk.ModifierType.CONTROL_MASK)
    assert view.selected_cell == (199, 9)
    assert area.props.vadjustment.get_value() > 0
    area.handle_key(Gdk.KEY_Home, Gdk.ModifierType.CONTROL_MASK)
    assert view.selected_cell == (0, 0) and area.props.vadjustment.get_value() == 0
    area.handle_key(Gdk.KEY_Up)
    assert view.selected_cell == (0, 0)
    assert not area.handle_key(Gdk.KEY_a)


# Loading states


def test_loading_note_while_the_engine_works(view):
    view.set_loading(True)
    assert view.loading and view._status.get_visible() and view._note.get_text() == "Loading spreadsheet…"
    i18n.set_language("ru")
    assert view._note.get_text() == "Загрузка таблицы…"
    i18n.set_language("en")
    view.set_loading(False)
    assert not view.loading and not view._status.get_visible()


def test_slow_sheet_loads_off_the_main_thread_and_late_grids_are_dropped(view):
    gate = threading.Event()
    book = Book(grid("A", cells={(0, 0): Cell("a")}), grid("B", cells={(0, 0): Cell("b")}), gate=gate)
    view.load(book)
    assert view.loading and view.selected_cell is None
    view.show_sheet(1)
    gate.set()
    assert pump(lambda: not view.loading)
    pump(lambda: False, 0.1)
    assert view.sheet_index == 1 and view.formula_text == "b"


def test_empty_and_broken_sheets_say_so(view):
    show(view, Book(grid("Empty", rows=0, cols=0), None))
    assert view._note.get_text() == "This sheet is empty" and view.selected_cell is None
    view.show_sheet(1)
    assert pump(lambda: not view.loading)
    assert view._note.get_text() == "This sheet cannot be shown"


# Find (6.2-6.5)


def find_book():
    first = grid("One", cells={(0, 1): Cell("Apple"), (2, 0): Cell("pineapple"), (3, 3): Cell("pear")})
    second = grid("Two", cells={(1, 1): Cell("APPLE pie"), (0, 0): Cell("nothing")})
    return Book(first, second)


def test_find_steps_through_every_sheet_and_wraps(view):
    show(view, find_book())
    assert view.find("apple") == (0, 3)
    assert (view.sheet_index, view.selected_cell) == (0, (0, 1))
    assert view.area.hits == {(0, 1), (2, 0)} and view.area.current_hit == (0, 1)
    assert view.find("apple") == (1, 3) and view.selected_cell == (2, 0)
    assert view.find("apple") == (2, 3)
    assert (view.sheet_index, view.selected_cell) == (1, (1, 1))
    assert view.area.hits == {(1, 1)} and view._tab_buttons[1].get_active()
    assert view.find("apple") == (0, 3) and view.sheet_index == 0
    assert view.find("apple", forward=False) == (2, 3) and view.sheet_index == 1
    assert view.find("apple", forward=False) == (1, 3) and view.selected_cell == (2, 0)


def test_find_starts_at_the_selected_cell(view):
    show(view, find_book())
    view.select_cell(2, 0)
    assert view.find("apple") == (1, 3)
    view.select_cell(3, 3)
    assert view.find("apple", forward=False) == (1, 3)


def test_find_case_and_no_matches(view):
    show(view, find_book())
    assert view.find("APPLE", case=True) == (0, 1) and view.sheet_index == 1
    assert view.find("пирог") == (-1, 0)
    assert view.find("") == (-1, 0) and view.area.hits == set()
    view.find("apple")
    view.clear()
    assert view.area.hits == set() and view.area.current_hit is None


def test_find_matches_russian_ignoring_case(view):
    show(view, Book(grid(cells={(1, 2): Cell("Итого за месяц")})))
    assert view.find("итого") == (0, 1) and view.selected_cell == (1, 2)


def test_view_is_a_find_backend(view):
    find = pytest.importorskip("sovander.find")
    assert isinstance(view, find.FindBackend)


# Memory


def test_closed_view_is_freed():
    # Handlers kept from C must not hold the view, or every closed workbook would stay in memory
    window = Gtk.Window()
    view = SheetView()
    window.set_child(view)
    window.present()
    view.load(find_book(), formula=lambda *_a: "=X()")
    assert pump(lambda v=view: not v.loading)
    view.select_cell(3, 3)
    view.find("apple")
    view.area.handle_key(Gdk.KEY_Down)
    view._tab_buttons[1].set_active(True)
    pump(lambda: False, 0.3)
    gone, area_gone = weakref.ref(view), weakref.ref(view.area)
    del view
    window.set_child(None)
    window.destroy()
    del window
    context = GLib.MainContext.default()
    for _ in range(50):
        context.iteration(False)
    gc.collect()
    assert gone() is None and area_gone() is None
    assert not [r for r in i18n._listeners if r() is not None]


# Performance (4.6): only the cells in view are drawn


class LazyCells(Mapping):
    """100,000 x 30 cells made on demand: a real dict of them takes about 840 MB."""

    def __init__(self, rows, cols):
        self.rows, self.cols = rows, cols

    def get(self, key, default=None):
        r, c = key
        if 0 <= r < self.rows and 0 <= c < self.cols:
            text = f"{r * 7 + c:,}.{c:02d}" if c % 3 else f"Item {r}"
            return Cell(text, "#FFF2CC" if r % 10 == 0 else None)
        return default

    def __getitem__(self, key):
        cell = self.get(key)
        if cell is None:
            raise KeyError(key)
        return cell

    def __iter__(self):
        return ((r, c) for r in range(self.rows) for c in range(self.cols))

    def __len__(self):
        return self.rows * self.cols


def frame(widget):
    """Seconds to build the widget's render nodes, and to render them as the window would."""
    start = time.perf_counter()
    snapshot = Gtk.Snapshot()
    widget.do_snapshot(snapshot)
    node = snapshot.to_node()
    built = time.perf_counter()
    bounds = Graphene.Rect().init(0, 0, widget.get_width(), widget.get_height())
    widget.get_native().get_renderer().render_texture(node, bounds)
    return built - start, time.perf_counter() - built


def test_100k_rows_first_paint_and_scrolling_are_fast(window):
    window.set_default_size(1280, 800)
    view = SheetView()
    window.set_child(view)
    window.present()
    assert pump(lambda: view.area.get_width() > 0)
    rows, cols = 100_000, 30
    big = Grid("Big", list(range(rows)), list(range(cols)), [17] * rows, [80] * cols, LazyCells(rows, cols))
    start = time.perf_counter()
    view.load(Book(big))
    assert pump(lambda: view.area.grid is big)
    loaded = time.perf_counter() - start
    build, render = frame(view.area)
    first = loaded + build + render

    vadj = view.area.props.vadjustment
    steps = []
    for k in range(1, 61):  # wheel-sized steps, then far jumps
        vadj.set_value(k * 51)
        steps.append(sum(frame(view.area)))
    jumps = []
    for value in (vadj.get_upper() / 2, vadj.get_upper() - vadj.get_page_size(), 0):
        vadj.set_value(value)
        jumps.append(sum(frame(view.area)))
    view.area.handle_key(Gdk.KEY_End, Gdk.ModifierType.CONTROL_MASK)
    assert view.selected_cell == (rows - 1, cols - 1) and view.area.row_label(rows - 1) == "100000"
    ms = [round(t * 1000, 1) for t in (first, loaded, build, render, sum(steps) / len(steps), max(steps))]
    print(f"\n100k x 30: first paint {ms[0]} ms (load {ms[1]}, nodes {ms[2]}, render {ms[3]});"
          f" scroll step avg {ms[4]} ms, max {ms[5]} ms; jumps max {max(jumps) * 1000:.1f} ms")
    assert first < 1.0
    assert sum(steps) / len(steps) < 0.05 and max(jumps) < 0.15


# Fast preview, then the engine's grid (4.6, task 4.8)


class Preview(Book):
    provisional = True


def test_preview_says_formatting_and_the_engine_grid_keeps_the_place(view):
    rows = 300
    fast = grid("Data", rows=rows, cols=5, cells={(r, 0): Cell(str(r)) for r in range(rows)})
    show(view, Preview(fast))
    assert view.formatting and view._marker.get_visible() and view._marker_label.get_text() == "Formatting…"
    view.select_cell(250, 3)
    top = view.area.props.vadjustment.get_value()
    assert top > 0
    gate = threading.Event()
    final = grid("Data", rows=rows, cols=5, cells={(r, 0): Cell(f"{r}.00") for r in range(rows)})
    view.load(Book(final, gate=gate))
    assert view.area.grid is fast and view.formatting  # the preview stays while the engine grid is parsed
    gate.set()
    assert pump(lambda: view.area.grid is final)
    assert not view.formatting and not view._marker.get_visible()
    assert view.selected_cell == (250, 3) and view.area.props.vadjustment.get_value() == top
    assert view.formula_text == ""
    view.select_cell(250, 0)
    assert view.formula_text == "250.00"


def test_set_formatting_off_hides_the_marker(view):
    show(view, Preview(grid()))
    view.set_formatting(False)
    assert not view._marker.get_visible()
