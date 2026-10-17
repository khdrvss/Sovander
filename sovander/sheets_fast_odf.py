"""Fast first screen for OpenDocument spreadsheets, .ods and flat .fods (task C7, Requirement 4.6).

content.xml is streamed with expat: the first PREVIEW_ROWS rows of every visible table, each cell as
the text the file stores for it (text:p, what the author's application displayed), else its raw
office:value. Repeated rows and cells are expanded only up to the preview's size, so an empty row
repeated 1,048,576 times costs one step. Once a table has its rows, the rest of it is skipped with a
byte search for the next <table:table> instead of being parsed (all sheets share one content.xml).
"""

from __future__ import annotations

import io
import re
import zipfile
from xml.parsers import expat

from sovander.engine.sheets import DEFAULT_HEIGHT, DEFAULT_WIDTH, Cell, Grid
from sovander.sheets_fast import MAX_TEXT, PREVIEW_ROWS, Preview, number_text

MAX_COLS = 512  # preview width: bounds a filled cell repeated over all 16,384 columns
_TABLE = "urn:oasis:names:tc:opendocument:xmlns:table:1.0"
_OFFICE = "urn:oasis:names:tc:opendocument:xmlns:office:1.0"
_STYLE = "urn:oasis:names:tc:opendocument:xmlns:style:1.0"
_TEXT = "urn:oasis:names:tc:opendocument:xmlns:text:1.0"
_MIME = b"application/vnd.oasis.opendocument.spreadsheet"
_ROOT = re.compile(rb"<(\w+):document(?:-content)?\b[^>]*>")
_NEXT_TABLE = re.compile(rb"<table:table[\s>]")
_CHUNK = 1 << 16


class _Skip(Exception):
    """The current table has its rows: the rest of it is not parsed."""


def is_odf_sheet(data: bytes) -> bool:
    return _MIME in data[:4096]


def preview(data: bytes, max_rows: int = PREVIEW_ROWS) -> Preview | None:
    """The first rows of an .ods/.fods workbook's visible tables, or None if this reader cannot read it."""
    try:
        if data.startswith(b"PK"):
            with zipfile.ZipFile(io.BytesIO(data)) as zf:
                if not zf.read("mimetype").startswith(_MIME):
                    return None
                with zf.open("content.xml") as stream:
                    return _Reader(max_rows).run(stream)
        if is_odf_sheet(data):
            return _Reader(max_rows).run(io.BytesIO(data))
    except (zipfile.BadZipFile, KeyError, expat.ExpatError, ValueError, OSError, RuntimeError):
        return None
    return None


class _Reader:
    def __init__(self, max_rows: int):
        self.max_rows = max_rows
        self.names: list[str] = []
        self.grids: list[Grid] = []
        self.hidden_styles: set[str] = set()
        self.style: str | None = None
        self.table: dict | None = None
        self.cell: list | None = None  # [repeat, value-type, value, text parts, paragraphs, characters]
        self.parser = None
        self.skip_at = 0  # byte index of the row that ended a table's preview
        self.note = 0  # inside office:annotation (a comment): not the cell's text

    def run(self, stream) -> Preview:
        chunk = stream.read(_CHUNK)
        root = _ROOT.search(chunk)
        ns = root.group(1) if root else b""
        prefix = root.group(0) + b"<%s:body><%s:spreadsheet>" % (ns, ns) if root else None
        lead = done = 0  # prefix bytes fed to this parser; stream bytes fed before this chunk
        self._new_parser()
        while chunk:
            try:
                self.parser.Parse(chunk, False)
            except _Skip:
                self._close_table()
                if prefix is None:
                    break
                tail = chunk[self.skip_at - lead - done + 1:]
                while not (found := _NEXT_TABLE.search(tail)) and (more := stream.read(_CHUNK)):
                    tail = tail[-16:] + more
                if not found:
                    break
                self._new_parser()
                self.parser.Parse(prefix, False)
                chunk, lead, done = tail[found.start():], len(prefix), 0
                continue
            done += len(chunk)
            chunk = stream.read(_CHUNK)
        self._close_table()
        return Preview(self.names, self.grids)

    def _new_parser(self) -> None:
        parser = expat.ParserCreate(namespace_separator=" ")
        parser.buffer_text = True
        parser.StartElementHandler, parser.EndElementHandler = self._start, self._end
        parser.CharacterDataHandler = self._chars
        self.parser = parser

    def _start(self, name: str, attrs: dict) -> None:
        ns, _, tag = name.rpartition(" ")
        table = self.table
        if ns == _OFFICE and tag == "annotation":
            self.note += 1
        elif ns == _TEXT and self.cell is not None and not self.note:
            if tag in ("p", "h"):
                self.cell[4] += 1
                if self.cell[4] > 1:
                    self._add("\n")
            elif tag == "s":
                self._add(" " * min(int(attrs.get(f"{_TEXT} c", "1")), MAX_TEXT))
            elif tag == "tab":
                self._add("\t")
            elif tag == "line-break":
                self._add("\n")
        elif ns != _TABLE:
            if ns == _STYLE and tag == "style":
                self.style = attrs.get(f"{_STYLE} name") if attrs.get(f"{_STYLE} family") == "table" else None
            elif ns == _STYLE and tag == "table-properties" and self.style:
                if attrs.get(f"{_TABLE} display") == "false":
                    self.hidden_styles.add(self.style)
        elif tag in ("table-cell", "covered-table-cell") and table is not None:
            value = attrs.get(f"{_OFFICE} value") or attrs.get(f"{_OFFICE} date-value") or attrs.get(
                f"{_OFFICE} time-value") or attrs.get(f"{_OFFICE} boolean-value") or ""
            self.cell = [_repeat(attrs, "columns"), attrs.get(f"{_OFFICE} value-type", ""), value, [], 0, 0]
        elif tag == "table-row" and table is not None:
            if table["row"] >= self.max_rows:
                self.skip_at = self.parser.CurrentByteIndex
                raise _Skip
            table["col"], table["repeat"] = 0, _repeat(attrs, "rows")
            table["row_hidden"] = attrs.get(f"{_TABLE} visibility", "visible") != "visible"
            table["line"] = {}
        elif tag == "table-column" and table is not None:
            repeat = _repeat(attrs, "columns")
            if attrs.get(f"{_TABLE} visibility", "visible") != "visible":
                table["hidden_cols"].update(range(table["column"], min(table["column"] + repeat, MAX_COLS)))
            table["column"] += repeat
        elif tag == "table":
            if table is not None:  # a table inside a cell: its text is not the sheet's
                table["nested"] += 1
                return
            self.table = {"name": attrs.get(f"{_TABLE} name", ""), "row": 0, "column": 0, "nested": 0,
                          "hidden": attrs.get(f"{_TABLE} style-name") in self.hidden_styles,
                          "cells": {}, "hidden_rows": set(), "hidden_cols": set()}

    def _end(self, name: str) -> None:
        ns, _, tag = name.rpartition(" ")
        if ns == _STYLE and tag == "style":
            self.style = None
        elif ns == _OFFICE and tag == "annotation":
            self.note -= 1
        if ns != _TABLE or self.table is None:
            return
        table = self.table
        if tag in ("table-cell", "covered-table-cell") and self.cell is not None:
            repeat, kind, value, parts, _paragraphs, _size = self.cell
            self.cell = None
            text = "".join(parts) or _raw(kind, value)
            col = table["col"]
            if text and not table["row_hidden"]:
                for c in range(col, min(col + repeat, MAX_COLS)):
                    if c not in table["hidden_cols"]:
                        table["line"][c] = text
            table["col"] = col + repeat
        elif tag == "table-row":
            row, repeat = table["row"], min(table["repeat"], self.max_rows - table["row"])
            if table["row_hidden"]:
                table["hidden_rows"].update(range(row, row + repeat))
            for r in range(row, row + repeat) if table["line"] else ():
                for c, text in table["line"].items():
                    table["cells"][(r, c)] = Cell(text)
            table["row"] += table["repeat"]
        elif tag == "table":
            if table["nested"]:
                table["nested"] -= 1
            else:
                self._close_table()

    def _chars(self, data: str) -> None:
        if self.cell is not None and self.cell[4] and not self.note:
            self._add(data)

    def _add(self, text: str) -> None:
        """Cell text up to MAX_TEXT characters: a crafted cell cannot grow without bound."""
        room = MAX_TEXT - self.cell[5]
        if room > 0:
            self.cell[3].append(text[:room])
            self.cell[5] += min(len(text), room)

    def _close_table(self) -> None:
        table, self.table, self.cell = self.table, None, None
        if table is None or table["hidden"]:
            return
        self.names.append(table["name"])
        self.grids.append(_grid(table["name"], table["cells"], table["hidden_rows"], table["hidden_cols"]))


def _repeat(attrs: dict, what: str) -> int:
    try:
        return max(1, int(attrs.get(f"{_TABLE} number-{what}-repeated", "1")))
    except ValueError:
        return 1


def _raw(kind: str, value: str) -> str:
    if not value:
        return ""
    if kind in ("float", "percentage", "currency"):
        return number_text(value)
    if kind == "boolean":
        return "TRUE" if value == "true" else "FALSE"
    return value.replace("T00:00:00", "")


def _grid(name: str, cells: dict, hidden_rows: set[int], hidden_cols: set[int]) -> Grid:
    if not cells:
        return Grid(name, [], [], [], [])
    first_row, last_row = min(r for r, _c in cells), max(r for r, _c in cells)
    first_col, last_col = min(c for _r, c in cells), max(c for _r, c in cells)
    rows = [r for r in range(first_row, last_row + 1) if r not in hidden_rows]
    cols = [c for c in range(first_col, last_col + 1) if c not in hidden_cols]
    return Grid(name, rows, cols, [DEFAULT_HEIGHT] * len(rows), [DEFAULT_WIDTH] * len(cols), cells)
