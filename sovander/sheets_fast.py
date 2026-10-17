"""Fast first screen for large spreadsheets (Requirement 4.6, task 4.8).

The engine needs 20+ s to load a big workbook (25 s for 150,000 x 20 cells, whose HTML export then
outlives the 60 s watchdog), so .xlsx/.xlsm/.xltx files are first read here, straight from the file
with the standard library: the first PREVIEW_ROWS rows of every visible sheet, values as stored.
Number formats are not reproduced (the engine applies them in its own locale), so numbers show raw
(at most 15 significant digits, as spreadsheets do), dates as ISO dates: never a wrong number. The
engine's formatted grid replaces this preview when it is ready; until then the sheet says
"Formatting…". Only stdlib: python3-openpyxl is in universe on 22.04/24.04 and would be a new Depends.
"""

from __future__ import annotations

import csv
import datetime
import io
import logging
import posixpath
import re
import zipfile
from xml.parsers import expat

from sovander.engine.sheets import DEFAULT_HEIGHT, DEFAULT_WIDTH, Cell, Grid
from sovander.i18n import N_

log = logging.getLogger(__name__)

PREVIEW_ROWS = 1000  # per sheet: a few screens, read in well under a second
# The file is untrusted and parsed in the app's own process: bounds a crafted one cannot pass
MAX_PART = 64 << 20  # a part read whole (shared strings, styles): larger ones are left to the engine
MAX_TEXT = 32767  # characters kept per cell, the most a spreadsheet cell holds
MAX_ROW, MAX_COL = 1 << 20, 1 << 14  # the sheet size of Excel and Calc
NOT_PREPARED = N_("The full spreadsheet could not be prepared. Showing the first rows as stored in the file.")
_MAIN = "http://schemas.openxmlformats.org/spreadsheetml/2006/main"
_REL = "http://schemas.openxmlformats.org/officeDocument/2006/relationships"
_PKG = "http://schemas.openxmlformats.org/package/2006/relationships"
_REF = re.compile(r"([A-Z]+)(\d+)")
_DATE_IDS = set(range(14, 18)) | {22} | set(range(27, 37)) | set(range(50, 59))
_TIME_IDS = {18, 19, 20, 21, 45, 46, 47}
_QUOTED = re.compile(r'"[^"]*"|\\.|\[(?![hms]+\])[^\]]*\]')


class _Enough(Exception):
    """Stops the sheet parser once the preview has its rows."""


class Preview:
    """The part of sheets.Workbook the sheet view uses, marked provisional (values not formatted)."""

    provisional = True

    def __init__(self, names: list[str], grids: list[Grid]):
        self.sheets, self._grids = names, grids

    def grid(self, index: int) -> Grid:
        return self._grids[index]


def first_screen(data: bytes, name: str) -> Preview | None:
    """The fast preview for any spreadsheet this module reads (.xlsx family, .ods/.fods, .csv/.tsv)."""
    ext = name.rsplit(".", 1)[-1].lower() if "." in name else ""
    if ext in ("csv", "tsv"):
        return text_preview(data, name)
    from sovander import sheets_fast_odf

    if sheets_fast_odf.is_odf_sheet(data):
        return sheets_fast_odf.preview(data)
    return preview(data)


def text_preview(data: bytes, name: str, max_rows: int = PREVIEW_ROWS) -> Preview:
    """The first rows of a .csv/.tsv file, never fails: separator and character set found as the engine
    finds them (engine.client), so the preview splits and decodes the file as the full sheet will."""
    from sovander.engine import client

    head = data[:1 << 16]
    encoding = client.csv_encoding(head)
    encoding = "utf-8-sig" if encoding == "utf-8" else encoding  # a byte order mark is not text
    tsv = name.lower().endswith(".tsv")
    delimiter = "\t" if tsv else client.csv_separator(head.decode(encoding, "replace"))
    cells: dict[tuple[int, int], Cell] = {}
    width = 0
    stream = io.TextIOWrapper(io.BytesIO(data), encoding=encoding, errors="replace", newline="")
    try:
        for row, fields in enumerate(csv.reader(stream, delimiter=delimiter)):
            if row >= max_rows:
                break
            for col, text in enumerate(fields[:1024]):
                if text:
                    cells[(row, col)] = Cell(text)
            width = max(width, min(len(fields), 1024))
    except csv.Error as exc:  # a NUL byte or a field over the csv module's limit: keep what was read
        log.info("csv preview stopped: %s", exc)
    rows = list(range(max((r for r, _c in cells), default=-1) + 1))
    cols = list(range(width)) if cells else []
    sheet = file_sheet_name(name)
    grid = Grid(sheet, rows, cols, [DEFAULT_HEIGHT] * len(rows), [DEFAULT_WIDTH] * len(cols), cells)
    return Preview([sheet], [grid])



def file_sheet_name(name: str) -> str:
    """A text file's one sheet is named after the file, as spreadsheet applications name it."""
    return posixpath.splitext(posixpath.basename(name.replace("\\", "/")))[0] or name

def preview(data: bytes, max_rows: int = PREVIEW_ROWS) -> Preview | None:
    """The first rows of an OOXML workbook's visible sheets, or None if this reader cannot read it."""
    if not data.startswith(b"PK"):
        return None
    try:
        with zipfile.ZipFile(io.BytesIO(data)) as zf:
            return _read(zf, max_rows)
    except (zipfile.BadZipFile, KeyError, expat.ExpatError, ValueError, OverflowError, OSError) as exc:
        log.info("no fast preview: %s", exc)
        return None


def _member(zf: zipfile.ZipFile, path: str) -> bytes:
    """A part read whole; zipfile never inflates past the declared size, so checking it bounds the read."""
    if zf.getinfo(path).file_size > MAX_PART:
        raise ValueError(f"{path} is too large for a preview")
    return zf.read(path)


def _parse(data, start=None, end=None, text=None) -> None:
    """Parse bytes or a file object (read in 64 KB chunks, so a handler can stop early)."""
    parser = expat.ParserCreate(namespace_separator=" ")
    parser.buffer_text = True
    parser.StartElementHandler, parser.EndElementHandler, parser.CharacterDataHandler = start, end, text
    if isinstance(data, bytes):
        parser.Parse(data, True)
        return
    with data:
        while chunk := data.read(1 << 16):
            parser.Parse(chunk, False)
    parser.Parse(b"", True)


def _read(zf: zipfile.ZipFile, max_rows: int) -> Preview | None:
    names = set(zf.namelist())
    if "xl/workbook.xml" not in names:
        return None
    sheets, rels, date1904 = [], {}, [False]

    def on_book(name, attrs):
        if name == f"{_MAIN} sheet" and attrs.get("state", "visible") == "visible":
            sheets.append((attrs.get("name", ""), attrs.get(f"{_REL} id")))
        elif name == f"{_MAIN} workbookPr":
            date1904[0] = attrs.get("date1904") in ("1", "true")

    _parse(_member(zf, "xl/workbook.xml"), on_book)

    def on_rel(name, attrs):
        if name == f"{_PKG} Relationship":
            target = attrs.get("Target", "")
            rels[attrs.get("Id")] = target.lstrip("/") if target.startswith("/") else posixpath.normpath(
                posixpath.join("xl", target))

    if "xl/_rels/workbook.xml.rels" in names:
        _parse(_member(zf, "xl/_rels/workbook.xml.rels"), on_rel)
    strings = _shared_strings(zf, names)
    styles = _styles(zf, names)
    epoch = datetime.datetime(1904, 1, 1) if date1904[0] else datetime.datetime(1899, 12, 30)
    grids = [_sheet(zf.open(rels[rid]), name, strings, styles, epoch, max_rows) for name, rid in sheets]
    return Preview([name for name, _rid in sheets], grids)


def _shared_strings(zf, names) -> list[str]:
    path = next((n for n in ("xl/sharedStrings.xml", "xl/SharedStrings.xml") if n in names), None)
    if path is None:
        return []
    out: list[str] = []
    state = {"t": False, "skip": 0, "parts": []}

    def start(name, _attrs):
        tag = name.rsplit(" ", 1)[-1]
        if tag == "si":
            state["parts"] = []
        elif tag == "t":
            state["t"] = True
        elif tag in ("rPh", "phoneticPr"):
            state["skip"] += 1

    def end(name):
        tag = name.rsplit(" ", 1)[-1]
        if tag == "si":
            out.append("".join(state["parts"]))
        elif tag == "t":
            state["t"] = False
        elif tag in ("rPh", "phoneticPr"):
            state["skip"] -= 1

    def text(data):
        if state["t"] and not state["skip"]:
            state["parts"].append(data)

    _parse(_member(zf, path), start, end, text)
    return out


def _styles(zf, names) -> list[tuple[str, str | None]]:
    """Per cell style index: (kind of value: "date", "time" or "", fill "#RRGGBB" or None)."""
    if "xl/styles.xml" not in names:
        return []
    formats: dict[int, str] = {}
    fills: list[str | None] = []
    xfs: list[tuple[int, int]] = []
    where = {"xfs": False, "fill": False}

    def start(name, attrs):
        tag = name.rsplit(" ", 1)[-1]
        if tag == "numFmt":
            formats[int(attrs.get("numFmtId", "0"))] = attrs.get("formatCode", "")
        elif tag == "cellXfs":
            where["xfs"] = True
        elif tag == "xf" and where["xfs"]:
            xfs.append((int(attrs.get("numFmtId", "0")), int(attrs.get("fillId", "0"))))
        elif tag == "fill":
            fills.append(None)
            where["fill"] = True
        elif tag == "patternFill" and where["fill"]:
            where["solid"] = attrs.get("patternType") == "solid"
        elif tag == "fgColor" and where["fill"] and where.get("solid") and len(attrs.get("rgb", "")) == 8:
            colour = "#" + attrs["rgb"][2:].upper()
            fills[-1] = None if colour == "#FFFFFF" else colour

    def end(name):
        tag = name.rsplit(" ", 1)[-1]
        if tag == "cellXfs":
            where["xfs"] = False
        elif tag == "fill":
            where["fill"] = where["solid"] = False

    _parse(_member(zf, "xl/styles.xml"), start, end)
    return [(_kind(fmt, formats.get(fmt, "")), fills[fill] if 0 <= fill < len(fills) else None)
            for fmt, fill in xfs]


def _kind(fmt_id: int, code: str) -> str:
    if fmt_id in _DATE_IDS:
        return "date"
    if fmt_id in _TIME_IDS:
        return "time"
    bare = _QUOTED.sub("", code.split(";")[0]).lower()
    if any(ch in bare for ch in "yd"):
        return "date"
    if any(ch in bare for ch in "hs") or ("m" in bare and not any(ch in bare for ch in "0#?")):
        return "time"
    return ""


def column_index(letters: str) -> int:
    index = 0
    for ch in letters:
        index = index * 26 + ord(ch) - 64
    return index - 1


def number_text(value: str) -> str:
    """A stored number as spreadsheets show at most: 15 significant digits, no float noise."""
    try:
        number = float(value)
    except ValueError:
        return value
    if number == int(number) and abs(number) < 1e15:
        return str(int(number))
    text = f"{number:.15g}"
    return text.upper() if "e" in text else text


def _date_text(value: str, kind: str, epoch: datetime.datetime) -> str:
    try:
        moment = epoch + datetime.timedelta(days=float(value))
    except (ValueError, OverflowError):
        return number_text(value)
    moment = (moment + datetime.timedelta(microseconds=500_000)).replace(microsecond=0)
    if kind == "time" and float(value) < 1:
        return moment.strftime("%H:%M:%S")
    return moment.strftime("%Y-%m-%d" if moment.time() == datetime.time() else "%Y-%m-%d %H:%M:%S")


def _sheet(xml, name: str, strings, styles, epoch, max_rows: int) -> Grid:
    """The first max_rows rows of one sheet; hidden rows and columns left out, as the engine does."""
    cells: dict[tuple[int, int], Cell] = {}
    formulas: dict[tuple[int, int], str] = {}
    shown_rows: dict[int, int] = {}
    hidden_cols: set[int] = set()
    hidden_rows: set[int] = set()
    widths: dict[int, int] = {}
    st = {"default_h": DEFAULT_HEIGHT, "default_w": DEFAULT_WIDTH, "row": -1, "rows": 0, "text": None,
          "cell": None, "hidden": False, "size": 0}

    def start(tag, attrs):
        tag = tag.rsplit(" ", 1)[-1]
        if tag == "c":
            ref = _REF.match(attrs.get("r", ""))
            col = column_index(ref.group(1)) if ref else st.get("next_col", 0)
            st["next_col"], st["size"] = col + 1, 0
            style = int(attrs.get("s", "0"))
            st["cell"] = [col, attrs.get("t", "n"), style, "", None] if col < MAX_COL else None
        elif tag in ("v", "t", "f") and st["cell"] is not None:
            st["text"] = [] if tag != "f" else None
            st["in"] = tag
            if tag == "f":
                st["formula"] = []
        elif tag == "row":
            if st["rows"] >= max_rows:
                raise _Enough
            row = int(attrs.get("r", st["row"] + 2)) - 1
            if not 0 <= row < MAX_ROW:  # past the sheet's last row: a damaged or crafted file
                raise _Enough
            st["row"], st["next_col"], st["rows"] = row, 0, st["rows"] + 1
            st["hidden"] = attrs.get("hidden") in ("1", "true")
            if st["hidden"]:
                hidden_rows.add(row)
            else:
                height = attrs.get("ht")
                shown_rows[row] = round(float(height) * 96 / 72) if height else st["default_h"]
        elif tag == "sheetFormatPr":
            if attrs.get("defaultRowHeight"):
                st["default_h"] = round(float(attrs["defaultRowHeight"]) * 96 / 72)
            if attrs.get("defaultColWidth"):
                st["default_w"] = _px(float(attrs["defaultColWidth"]))
        elif tag == "col":
            span = range(int(attrs.get("min", "1")) - 1, int(attrs.get("max", "1")))
            if len(span) > 16384:
                return
            for col in span:
                if attrs.get("hidden") in ("1", "true"):
                    hidden_cols.add(col)
                elif attrs.get("width"):
                    widths[col] = _px(float(attrs["width"]))

    def end(tag):
        tag = tag.rsplit(" ", 1)[-1]
        if tag in ("v", "t") and st.get("in") == tag:
            st["cell"][3] += "".join(st["text"])
            st["in"] = None
        elif tag == "f" and st.get("in") == "f":
            st["cell"][4] = "".join(st["formula"])
            st["in"] = None
        elif tag == "c" and st["cell"] is not None:
            col, kind, style, raw, formula = st["cell"]
            st["cell"] = None
            if st["hidden"] or col in hidden_cols:
                return
            value_kind, fill = styles[style] if 0 <= style < len(styles) else ("", None)
            text = _value(kind, raw, value_kind, strings, epoch)
            key = (st["row"], col)
            if text or fill:
                cells[key] = Cell(text, fill)
            if formula:
                formulas[key] = "=" + formula.replace("_xlfn.", "").replace("_xlws.", "")

    def chars(data):
        if st.get("in") not in ("v", "t", "f") or st["size"] >= MAX_TEXT:
            return
        (st["formula"] if st["in"] == "f" else st["text"]).append(data[:MAX_TEXT - st["size"]])
        st["size"] += len(data)

    try:
        _parse(xml, start, end, chars)
    except _Enough:
        pass
    if not cells:
        return Grid(name, [], [], [], [])
    first_row, last_row = min(r for r, _c in cells), max(r for r, _c in cells)
    first_col, last_col = min(c for _r, c in cells), max(c for _r, c in cells)
    rows = [r for r in range(first_row, last_row + 1) if r not in hidden_rows]
    cols = [c for c in range(first_col, last_col + 1) if c not in hidden_cols]
    return Grid(name, rows, cols, [shown_rows.get(r, st["default_h"]) for r in rows],
                [widths.get(c, st["default_w"]) for c in cols], cells, formulas)


def _px(chars: float) -> int:
    """Excel column width in characters to pixels (Calibri 11: 7 px per digit, 5 px padding)."""
    return int(chars * 7 + 5) if chars else 0


def _value(kind: str, raw: str, value_kind: str, strings: list[str], epoch) -> str:
    if kind == "s":
        try:
            return strings[int(raw)]
        except (ValueError, IndexError):
            return ""
    if kind in ("inlineStr", "str", "e"):
        return raw
    if kind == "b":
        return "TRUE" if raw.strip() in ("1", "true") else "FALSE"
    if not raw:
        return ""
    if kind == "d":
        return raw.replace("T00:00:00", "")
    return _date_text(raw, value_kind, epoch) if value_kind else number_text(raw)


def sheet_job(post, alive, source, password: str | None, context) -> None:
    """open_jobs.sheet_job with a fast first screen: the preview at once, the engine's grid after it.

    If the engine then fails (60 s watchdog on a huge sheet, damage, no engine), the preview stays
    with a note instead of an error page.
    """
    import functools

    from sovander.engine import macros, sheets
    from sovander.open_jobs import MACROS_NOTE

    data = source.read()
    if macros.has_macros(data):
        post("_add_note", "macros", MACROS_NOTE)
    first = None
    if password is None:
        try:
            first = first_screen(data, source.name)
        except Exception:  # noqa: BLE001 - a preview that fails is no preview: the engine still opens the file
            log.warning("%s: no fast preview", source.name, exc_info=True)
    if first is not None:
        post("_show_workbook", first, None)
    engine = context.engine
    try:
        book = sheets.workbook(data, password, engine)
    except Exception:  # noqa: BLE001 - the preview stays; the reason is logged
        if first is None or not alive():
            raise
        log.warning("%s: engine grid not ready, keeping the preview", source.name, exc_info=True)
        post("_sheet_unformatted", NOT_PREPARED)
        return
    if alive():
        # a CSV reaches the engine as a stream without a file name, so its sheet comes back unnamed
        book.sheets = [sheet or file_sheet_name(source.name) for sheet in book.sheets]
        formula = functools.partial(sheets.formula, data, password=password, engine=engine)
        post("_show_workbook", book, formula)
