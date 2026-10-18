"""Spreadsheet grids from the engine's HTML export (Requirements 2.4, 4.1–4.5).

Calc's HTML export writes each visible sheet with values as displayed (number formats applied),
fills, merges and column widths. It skips hidden rows, columns and sheets and starts at the first
used cell, so the real cell coordinates come from the same loaded document over UNO. Values are
never recalculated: the engine profile sets Calc's recalc-on-load modes to "never" (4.2).
"""

from __future__ import annotations

import logging
import re
from collections.abc import Iterator, Mapping
from dataclasses import dataclass, field
from html.parser import HTMLParser
from pathlib import Path
from typing import Any

from . import process
from .client import SHEET, ConversionError, close_document, export, open_document, read_source
from .process import Engine

log = logging.getLogger(__name__)

HTML_FILTER = "HTML (StarCalc)"
_ANCHOR = re.compile(r'<a name="table(\d+)">', re.IGNORECASE)
_ROW = re.compile(r"<tr\b", re.IGNORECASE)
_COLGROUP = re.compile(r'<colgroup\b(?:[^>]*?\bspan="?(\d+))?', re.IGNORECASE)
_FORMULA_CELLS = 16  # css.sheet.CellFlags.FORMULA
_CONTENT_CELLS = 1 | 2 | 4 | 16  # VALUE | DATETIME | STRING | FORMULA
DEFAULT_WIDTH, DEFAULT_HEIGHT = 64, 17
# The export writes every cell of a sheet's used area, empty or not (30-100 bytes each), and parsing
# takes about 0.3 µs a byte: one far-off cell in a small file would cost minutes and gigabytes (NFR)
MAX_HTML = 128 << 20


@dataclass(slots=True)
class Cell:
    text: str  # as displayed by the file's own number format (4.3)
    fill: str | None = None  # "#RRGGBB"; white counts as no fill
    rowspan: int = 1  # in shown rows and columns: hidden ones inside a merge are not counted
    colspan: int = 1


_SHIFT = 24  # packed key = row << 24 | col; sheets have at most 16,384 columns


class CellStore(Mapping):
    """(row, col) -> Cell, stored compactly for sheets with millions of cells (NFR memory).

    A plain cell (no fill, no merge) is kept as its text alone under one packed int key, about
    a third of a dict of tuple keys to Cell objects; Cell objects are made when a cell is read.
    Fill strings are shared. Reading returns a fresh Cell: change cells through the store.
    """

    __slots__ = ("_cells", "_fills")

    def __init__(self, cells: Mapping[tuple[int, int], Cell] | None = None):
        self._cells: dict[int, str | Cell] = {}
        self._fills: dict[str, str] = {}
        for key, cell in (cells or {}).items():
            self[key] = cell

    def __setitem__(self, key: tuple[int, int], cell: Cell) -> None:
        row, col = key
        if cell.fill is None and cell.rowspan == 1 and cell.colspan == 1:
            self._cells[row << _SHIFT | col] = cell.text
        else:
            fill = cell.fill and self._fills.setdefault(cell.fill, cell.fill)
            self._cells[row << _SHIFT | col] = Cell(cell.text, fill, cell.rowspan, cell.colspan)

    def __getitem__(self, key: tuple[int, int]) -> Cell:
        row, col = key
        value = self._cells[row << _SHIFT | col]
        return Cell(value) if value.__class__ is str else Cell(*_fields(value))

    def get(self, key: tuple[int, int], default: Any = None) -> Any:
        row, col = key
        value = self._cells.get(row << _SHIFT | col)
        if value is None:
            return default
        return Cell(value) if value.__class__ is str else Cell(*_fields(value))

    def __contains__(self, key: object) -> bool:
        try:
            row, col = key  # type: ignore[misc]
            return (row << _SHIFT | col) in self._cells
        except (TypeError, ValueError):
            return False

    def __iter__(self) -> Iterator[tuple[int, int]]:
        mask = (1 << _SHIFT) - 1
        return ((key >> _SHIFT, key & mask) for key in self._cells)

    def __len__(self) -> int:
        return len(self._cells)

    def items(self):  # type: ignore[override]  # one pass, no lookups back into the dict
        mask = (1 << _SHIFT) - 1
        for key, value in self._cells.items():
            cell = Cell(value) if value.__class__ is str else Cell(*_fields(value))
            yield (key >> _SHIFT, key & mask), cell

    def values(self):  # type: ignore[override]
        for value in self._cells.values():
            yield Cell(value) if value.__class__ is str else Cell(*_fields(value))

    def __repr__(self) -> str:
        return f"CellStore({dict(self.items())!r})"


def _fields(cell: Cell) -> tuple[str, str | None, int, int]:
    return cell.text, cell.fill, cell.rowspan, cell.colspan


@dataclass
class Grid:
    """One sheet. Hidden rows and columns are absent; keys and lists use real sheet coordinates."""

    name: str
    rows: list[int]  # sheet row index (0-based) of each shown row
    cols: list[int]  # sheet column index (0-based) of each shown column
    row_heights: list[int]  # pixels, parallel to rows
    col_widths: list[int]  # pixels, parallel to cols
    cells: Mapping[tuple[int, int], Cell] = field(default_factory=CellStore)  # (row, col) -> merge top-left
    formulas: dict[tuple[int, int], str] = field(default_factory=dict)  # stored formulas (4.5)

    def __post_init__(self) -> None:
        if not isinstance(self.cells, CellStore):  # a plain dict from a caller: store it compactly
            self.cells = CellStore(self.cells)


@dataclass
class _SheetInfo:
    name: str
    table: str  # this sheet's part of the export ("" for an empty sheet)
    rows: list[int]
    cols: list[int]
    formulas: dict[tuple[int, int], str]


class Workbook:
    """Every visible sheet of one workbook; a sheet's grid is parsed when first asked for."""

    def __init__(self, sheets: list[_SheetInfo]):
        self._info = sheets
        self._grids: dict[int, Grid] = {}
        self.sheets = [info.name for info in sheets]  # tab names, in order (4.1)

    def grid(self, index: int) -> Grid:
        if index not in self._grids:
            info = self._info[index]
            self._grids[index] = _build_grid(info)
            info.table = ""  # the export's HTML is no longer needed once its grid is built
        return self._grids[index]


def workbook(source: Path | bytes, password: str | None = None, engine: Engine | None = None) -> Workbook:
    """Load a spreadsheet once and export all visible sheets as HTML grids."""
    data = read_source(source)
    engine = engine or process.shared()

    def job(eng: Engine) -> list[_SheetInfo]:
        doc, _handler = open_document(eng, data, SHEET, password)
        try:
            if not doc.supportsService("com.sun.star.sheet.SpreadsheetDocument"):
                raise ConversionError("This file is not a spreadsheet.")
            html = export(eng, doc, HTML_FILTER, filter_options="SkipImages")
            if len(html) > MAX_HTML:
                raise ConversionError("This spreadsheet is too large to show.")
            return _sheet_infos(doc, _split_tables(html.decode("utf-8", "replace")))
        finally:
            close_document(doc)

    return Workbook(engine.run(job))


def formula(source: Path | bytes, sheet: str | int, row: int, col: int, password: str | None = None,
            engine: Engine | None = None) -> str | None:
    """The stored formula of one cell (sheet by name or document index), or None for a plain value."""
    data = read_source(source)
    engine = engine or process.shared()

    def job(eng: Engine) -> str | None:
        doc, _handler = open_document(eng, data, SHEET, password)
        try:
            sheets = doc.getSheets()
            target = sheets.getByName(sheet) if isinstance(sheet, str) else sheets.getByIndex(sheet)
            cell = target.getCellByPosition(col, row)
            return cell.getFormula() if cell.getType().value == "FORMULA" else None
        finally:
            close_document(doc)

    return engine.run(job)


def _sheet_infos(doc: Any, tables: dict[int | None, str]) -> list[_SheetInfo]:
    """Visible sheets with their table, real row/column numbers and formulas.

    The export covers GetDataStart..GetPrintArea minus hidden rows and columns; the UNO cursor's
    start is the same GetDataStart, but its end is not, so the size comes from the table itself.
    """
    sheets = doc.getSheets()
    visible = [i for i in range(sheets.getCount()) if sheets.getByIndex(i).IsVisible]
    if None in tables:  # a lone exported table carries no anchor: it is the one sheet with content
        lone = tables.pop(None)
        owner = next((i for i in visible
                      if sheets.getByIndex(i).queryContentCells(_CONTENT_CELLS).getCount()), None)
        if owner is None and visible:
            owner = visible[0]
        tables[owner] = lone
    infos = []
    for index in visible:  # Excel shows no tab for a hidden sheet either
        sheet = sheets.getByIndex(index)
        table = tables.get(index, "")
        n_rows = len(_ROW.findall(table))
        n_cols = sum(_int(m.group(1), 1) for m in _COLGROUP.finditer(table))
        rows: list[int] = []
        cols: list[int] = []
        formulas: dict[tuple[int, int], str] = {}
        if n_rows and n_cols:
            cursor = sheet.createCursor()
            cursor.gotoStartOfUsedArea(False)
            start = cursor.getRangeAddress()
            rows, cols = _visible_lines(sheet, start.StartRow, start.StartColumn, n_rows, n_cols)
            if rows and cols:
                formulas = _formulas(sheet, rows[0], cols[0], rows[-1], cols[-1])
        infos.append(_SheetInfo(sheet.getName(), table, rows, cols, formulas))
    return infos


def _visible_lines(sheet: Any, row0: int, col0: int, n_rows: int, n_cols: int) -> tuple[list[int], list[int]]:
    """The first n_rows visible rows and n_cols visible columns from (row0, col0)."""
    last_row, last_col = sheet.getRows().getCount() - 1, sheet.getColumns().getCount() - 1
    extra = 0
    while True:  # widen the window until hidden rows/columns inside it are made up for
        row1, col1 = min(last_row, row0 + n_rows - 1 + extra), min(last_col, col0 + n_cols - 1 + extra)
        rows: set[int] = set()
        cols: set[int] = set()
        window = sheet.getCellRangeByPosition(col0, row0, col1, row1)
        for area in window.queryVisibleCells().getRangeAddresses():
            rows.update(range(area.StartRow, area.EndRow + 1))
            cols.update(range(area.StartColumn, area.EndColumn + 1))
        if (len(rows) >= n_rows or row1 == last_row) and (len(cols) >= n_cols or col1 == last_col):
            return sorted(rows)[:n_rows], sorted(cols)[:n_cols]
        extra = max(64, extra * 4)


def _formulas(sheet: Any, row0: int, col0: int, row1: int, col1: int) -> dict[tuple[int, int], str]:
    formulas = {}
    area_range = sheet.getCellRangeByPosition(col0, row0, col1, row1)
    for area in area_range.queryContentCells(_FORMULA_CELLS).getRangeAddresses():
        block = sheet.getCellRangeByPosition(area.StartColumn, area.StartRow, area.EndColumn, area.EndRow)
        for r, line in enumerate(block.getFormulaArray()):
            for c, text in enumerate(line):
                formulas[(area.StartRow + r, area.StartColumn + c)] = text
    return formulas


def _split_tables(html: str) -> dict[int | None, str]:
    """HTML of each exported sheet by document index; empty sheets have no table.

    Calc writes "tableN" anchors only when it exports more than one sheet; a lone table gets key None.
    """
    marks = list(_ANCHOR.finditer(html))
    if not marks:
        return {None: html} if "<table" in html.lower() else {}
    return {int(m.group(1)): html[m.end(): marks[i + 1].start() if i + 1 < len(marks) else len(html)]
            for i, m in enumerate(marks)}


def _build_grid(info: _SheetInfo) -> Grid:
    parser = _TableParser()
    parser.feed(info.table)
    parser.close()
    rows, cols = info.rows, info.cols
    width = max((sum(c["colspan"] for c in row) for row in parser.rows), default=0)
    if len(parser.rows) != len(rows) or max(width, len(parser.widths)) != len(cols):
        # cannot happen while the export keeps its format; number from the origin rather than fail
        log.warning("sheet %r: table is %dx%d, sheet area %dx%d", info.name, len(parser.rows),
                    max(width, len(parser.widths)), len(rows), len(cols))
        r0, c0 = (rows[0] if rows else 0), (cols[0] if cols else 0)
        rows = list(range(r0, r0 + len(parser.rows)))
        cols = list(range(c0, c0 + max(width, len(parser.widths))))
    widths = (parser.widths + [DEFAULT_WIDTH] * len(cols))[: len(cols)]
    cells = CellStore()
    grid = Grid(info.name, rows, cols, parser.heights[: len(rows)], widths, cells, info.formulas)
    covered: set[tuple[int, int]] = set()
    for r, row in enumerate(parser.rows):
        c = 0
        for cell in row:
            while (r, c) in covered:
                c += 1
            span_r, span_c = cell["rowspan"], cell["colspan"]
            if span_r > 1 or span_c > 1:
                covered.update((r + dr, c + dc) for dr in range(span_r) for dc in range(span_c))
            if (cell["text"] or cell["fill"] or span_r > 1 or span_c > 1) and r < len(rows) and c < len(cols):
                cells[(rows[r], cols[c])] = Cell(cell["text"], cell["fill"], span_r, span_c)
            c += span_c
    return grid


class _TableParser(HTMLParser):
    """Collects rows of {text, fill, rowspan, colspan}, column widths and row heights of one table."""

    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.rows: list[list[dict[str, Any]]] = []
        self.widths: list[int] = []
        self.heights: list[int] = []
        self._cell: dict[str, Any] | None = None
        self._text: list[str] = []
        self._hidden = 0  # inside a cell comment
        self._spread: list[int] = []  # height still owed to the rows of a vertical merge

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        a = {k: v or "" for k, v in attrs}
        if tag in ("colgroup", "col") and "width" in a:
            self.widths += [_int(a["width"], DEFAULT_WIDTH)] * max(1, _int(a.get("span"), 1))
        elif tag == "tr":
            self.rows.append([])
            self.heights.append(self._spread.pop(0) if self._spread else 0)
        elif tag in ("td", "th") and self.rows:
            span_r, span_c = max(1, _int(a.get("rowspan"), 1)), max(1, _int(a.get("colspan"), 1))
            if "height" in a and not self.heights[-1]:
                each = _int(a["height"], DEFAULT_HEIGHT) // span_r
                self.heights[-1] = each
                self._spread = [each] * (span_r - 1)
            fill = a.get("bgcolor", "").upper() or None
            self._cell = {"fill": None if fill == "#FFFFFF" else fill, "rowspan": span_r, "colspan": span_c}
            self._text = []
        elif tag == "br" and self._cell is not None and not self._hidden:
            self._text.append("\n")
        elif tag == "comment":
            self._hidden += 1

    def handle_endtag(self, tag: str) -> None:
        if tag == "comment" and self._hidden:
            self._hidden -= 1
        elif tag in ("td", "th") and self._cell is not None:
            self._cell["text"] = "".join(self._text).rstrip("\n")
            self.rows[-1].append(self._cell)
            self._cell = None
        elif tag == "table":  # rows without a height of their own repeat the one above
            for i, height in enumerate(self.heights):
                if not height:
                    self.heights[i] = self.heights[i - 1] if i else DEFAULT_HEIGHT

    def handle_data(self, data: str) -> None:
        if self._cell is not None and not self._hidden:
            self._text.append(data)


def _int(value: str | None, default: int) -> int:
    try:
        return int(value) if value else default
    except ValueError:
        return default
