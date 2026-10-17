"""The fast preview reader (task 4.8): values as stored, never formatted wrongly."""

import io
import time
import zipfile

import pytest

from sovander.sheets_fast import number_text, preview

MAIN = "http://schemas.openxmlformats.org/spreadsheetml/2006/main"
REL = "http://schemas.openxmlformats.org/officeDocument/2006/relationships"
PKG = "http://schemas.openxmlformats.org/package/2006/relationships"
STYLES = (f'<styleSheet xmlns="{MAIN}"><numFmts><numFmt numFmtId="164" formatCode="yyyy\\-mm\\-dd"/>'
          '<numFmt numFmtId="165" formatCode="#,##0.00&quot; EUR&quot;"/></numFmts>'
          '<fills><fill><patternFill patternType="none"/></fill>'
          '<fill><patternFill patternType="gray125"/></fill>'
          '<fill><patternFill patternType="solid"><fgColor rgb="FFFFCC00"/></patternFill></fill></fills>'
          '<cellXfs><xf numFmtId="0" fillId="0"/><xf numFmtId="164" fillId="0"/>'
          '<xf numFmtId="165" fillId="2"/>'
          '<xf numFmtId="14" fillId="0"/></cellXfs></styleSheet>')


def xlsx(sheet_xml: str, strings=("Hello", "Привет"), hidden_sheet=False) -> bytes:
    sheets = '<sheet name="Data" sheetId="1" r:id="rId1"/>'
    if hidden_sheet:
        sheets = '<sheet name="Secret" sheetId="2" state="hidden" r:id="rId1"/>' + sheets
    parts = {
        "xl/workbook.xml": f'<workbook xmlns="{MAIN}" xmlns:r="{REL}"><sheets>{sheets}</sheets></workbook>',
        "xl/_rels/workbook.xml.rels": f'<Relationships xmlns="{PKG}"><Relationship Id="rId1" '
        f'Type="{REL}/worksheet" Target="worksheets/sheet1.xml"/></Relationships>',
        "xl/sharedStrings.xml": f'<sst xmlns="{MAIN}">' + "".join(f"<si><t>{s}</t></si>" for s in strings)
        + "</sst>",
        "xl/styles.xml": STYLES,
        "xl/worksheets/sheet1.xml": f'<worksheet xmlns="{MAIN}">{sheet_xml}</worksheet>',
    }
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as zf:
        for name, text in parts.items():
            zf.writestr(name, text)
    return buf.getvalue()


def test_values_as_stored_with_real_coordinates():
    data = xlsx('<sheetFormatPr defaultRowHeight="15"/>'
                '<cols><col min="2" max="2" width="20" customWidth="1"/>'
                '<col min="4" max="4" hidden="1"/></cols><sheetData>'
                '<row r="2"><c r="B2" t="s"><v>0</v></c><c r="C2"><v>0.30000000000000004</v></c>'
                '<c r="D2"><v>7</v></c><c r="E2" s="2"><v>1234.5</v></c></row>'
                '<row r="3" hidden="1"><c r="B3"><v>1</v></c></row>'
                '<row r="4" ht="30" customHeight="1"><c r="B4" t="s"><v>1</v></c>'
                '<c r="C4" s="1"><v>46312</v></c>'
                '<c r="E4" t="b"><v>1</v></c><c r="F4" t="e"><v>#N/A</v></c>'
                '<c r="G4"><f>A1*10</f><v>999</v></c><c r="H4" t="inlineStr"><is><t>inline</t></is></c></row>'
                '</sheetData>')
    book = preview(data)
    assert book.provisional and book.sheets == ["Data"]
    grid = book.grid(0)
    assert grid.rows == [1, 3] and grid.cols == [1, 2, 4, 5, 6, 7]  # hidden row 3 and column D left out
    assert grid.row_heights == [20, 40] and grid.col_widths[:2] == [145, 64]
    text = {key: cell.text for key, cell in grid.cells.items()}
    assert text[(1, 1)] == "Hello" and text[(3, 1)] == "Привет"
    assert text[(1, 2)] == "0.3"  # no float noise
    assert text[(1, 4)] == "1234.5" and grid.cells[(1, 4)].fill == "#FFCC00"  # raw: format left to the engine
    assert text[(3, 2)] == "2026-10-17"  # a date stays a date, ISO
    assert (text[(3, 4)], text[(3, 5)], text[(3, 6)], text[(3, 7)]) == ("TRUE", "#N/A", "999", "inline")
    assert grid.formulas == {(3, 6): "=A1*10"}  # the stored value, not a recalculation


def test_hidden_sheets_have_no_tab():
    book = preview(xlsx('<sheetData><row r="1"><c r="A1"><v>1</v></c></row></sheetData>', hidden_sheet=True))
    assert book.sheets == ["Data"]


@pytest.mark.parametrize(("raw", "shown"), [("4200", "4200"), ("-3.5", "-3.5"), ("1E+20", "1E+20"),
                                             ("0.1", "0.1"), ("123456789012.345", "123456789012.345")])
def test_number_text(raw, shown):
    assert number_text(raw) == shown


@pytest.mark.parametrize("data", [b"", b"not a zip", b"PK\x03\x04broken", b"a;b\n1;2\n"])
def test_unreadable_files_have_no_preview(data):
    assert preview(data) is None


def test_150k_rows_preview_is_fast(fixture_file):
    rows = "".join(f'<row r="{r}">' + "".join(f'<c r="{c}{r}"><v>{r * 3 + i}</v></c>'
                                               for i, c in enumerate("ABCDEFGHIJKLMNOPQRST")) + "</row>"
                   for r in range(1, 150_001))
    data = xlsx(f"<sheetData>{rows}</sheetData>")
    start = time.perf_counter()
    grid = preview(data).grid(0)
    took = time.perf_counter() - start
    assert len(grid.rows) == 1000 and len(grid.cols) == 20
    assert grid.cells[(999, 19)].text == str(1000 * 3 + 19)
    assert took < 1.0, took


def test_fixture_workbook(fixture_file):
    book = preview(fixture_file("budget.xlsx").read_bytes())
    assert book.sheets == ["Summary", "Detail", "Notes"]
    assert book.grid(0).cells[(0, 0)].text == "Item" and book.grid(0).cells[(1, 2)].text == "4200"


def test_crafted_coordinates_sizes_and_text_stay_bounded():
    """The file is parsed in the app's own process: a crafted one must not hang it or eat memory."""
    far = preview(xlsx('<sheetData><row r="1"><c r="A1"><v>1</v></c><c r="ZZZZZZZZZZ1"><v>2</v></c></row>'
                       '<row r="999999999999"><c r="A999999999999"><v>3</v></c></row></sheetData>'))
    grid = far.grid(0)
    assert grid.rows == [0] and grid.cols == [0]  # past column XFD or row 1,048,576: dropped
    long = preview(xlsx(f'<sheetData><row r="1"><c r="A1" t="inlineStr"><is><t>{"x" * 100_000}</t></is></c>'
                        '</row></sheetData>')).grid(0)
    assert len(long.cells[(0, 0)].text) == 32767
    assert preview(xlsx('<sheetData><row r="1" ht="inf"><c r="A1"><v>1</v></c></row></sheetData>')) is None


def test_parts_larger_than_the_limit_are_left_to_the_engine(monkeypatch):
    from sovander import sheets_fast

    data = xlsx('<sheetData><row r="1"><c r="A1" t="s"><v>0</v></c></row></sheetData>', strings=["x" * 5000])
    assert preview(data) is not None
    monkeypatch.setattr(sheets_fast, "MAX_PART", 4096)  # the shared strings now count as a zip bomb
    assert preview(data) is None


def test_a_failing_preview_never_stops_the_engine_opening_the_file(monkeypatch):
    from types import SimpleNamespace

    from sovander import sheets_fast
    from sovander.engine import sheets

    book = SimpleNamespace(sheets=["Data"])
    monkeypatch.setattr(sheets_fast, "first_screen", lambda data, name: [][0])
    monkeypatch.setattr(sheets, "workbook", lambda data, password, engine: book)
    posted = []
    source = SimpleNamespace(name="book.xlsx", read=lambda: b"PK not really")
    sheets_fast.sheet_job(lambda *args: posted.append(args), lambda: True, source, None,
                          SimpleNamespace(engine=None))
    assert [args[:2] for args in posted] == [("_show_workbook", book)]


def test_an_unnamed_engine_sheet_takes_the_file_name(monkeypatch):
    """A CSV reaches LibreOffice as a stream without a name, so its sheet came back as an empty tab."""
    from types import SimpleNamespace

    from sovander import sheets_fast
    from sovander.engine import sheets

    book = SimpleNamespace(sheets=[""])
    monkeypatch.setattr(sheets, "workbook", lambda data, password, engine: book)
    posted = []
    source = SimpleNamespace(name="/home/user/goroda.csv", read=lambda: b"a;b\n1;2\n")
    sheets_fast.sheet_job(lambda *args: posted.append(args), lambda: True, source, None,
                          SimpleNamespace(engine=None))
    assert [args[1].sheets for args in posted] == [["goroda"], ["goroda"]]  # the preview, then the full sheet
