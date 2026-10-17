import io
import zipfile

import pytest

from sovander.engine import client
from sovander.engine.client import ConversionError
from sovander.engine.sheets import _TableParser, formula, workbook

pytestmark = pytest.mark.engine

MAIN = "http://schemas.openxmlformats.org/spreadsheetml/2006/main"
REL = "http://schemas.openxmlformats.org/officeDocument/2006/relationships"
PKG = "http://schemas.openxmlformats.org/package/2006/relationships"
# Workbooks the spike found coloured cells in (spike/results/b-libreoffice.json): regression floor 14/14
SPIKE_COLOURED = ["xlsx/HsGetVal.xlsx", "xlsx/bug60848_sumproduct_unary_minus.xlsx",
                  "xlsx/simple-monthly-budget.xlsx", "xlsx/46535.xlsx", "xlsx/45540_classic_Footer.xlsx",
                  "xlsx/NumberFormatTests.xlsx", "xlsx/61652.xlsx", "xlsx/57171_57163_57165.xlsx",
                  "gander/Q3 Operating Budget FY26.xlsx", "xlsx/49156.xlsx",
                  "xlsx/FormulaEvalTestData_Copy.xlsx",
                  "xlsx/45540_form_Footer.xlsx", "xlsx/CVLKRA-KYC_Download_File_Structure_V3.1.xlsx",
                  "xlsx/54288-ref.xlsx"]


def stale_formula_xlsx() -> bytes:
    """B1 holds =A1*10 with a stored result of 999: only a recalculation would show 20."""
    parts = {
        "[Content_Types].xml": '<Types xmlns="http://schemas.openxmlformats.org/package/2006/content-types">'
        '<Default Extension="rels" ContentType="application/vnd.openxmlformats-package.relationships+xml"/>'
        '<Default Extension="xml" ContentType="application/xml"/>'
        '<Override PartName="/xl/workbook.xml" ContentType="application/'
        'vnd.openxmlformats-officedocument.spreadsheetml.sheet.main+xml"/>'
        '<Override PartName="/xl/worksheets/sheet1.xml" ContentType="application/'
        'vnd.openxmlformats-officedocument.spreadsheetml.worksheet+xml"/></Types>',
        "_rels/.rels": f'<Relationships xmlns="{PKG}"><Relationship Id="rId1" '
        f'Type="{REL}/officeDocument" Target="xl/workbook.xml"/></Relationships>',
        "xl/workbook.xml": f'<workbook xmlns="{MAIN}" xmlns:r="{REL}"><sheets>'
        '<sheet name="Sheet1" sheetId="1" r:id="rId1"/></sheets></workbook>',
        "xl/_rels/workbook.xml.rels": f'<Relationships xmlns="{PKG}"><Relationship Id="rId1" '
        f'Type="{REL}/worksheet" Target="worksheets/sheet1.xml"/></Relationships>',
        "xl/worksheets/sheet1.xml": f'<worksheet xmlns="{MAIN}"><sheetData><row r="1">'
        '<c r="A1"><v>2</v></c><c r="B1"><f>A1*10</f><v>999</v></c></row></sheetData></worksheet>',
    }
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as zf:
        for name, text in parts.items():
            zf.writestr(name, text)
    return buf.getvalue()


@pytest.fixture(scope="module")
def layout_xlsx(engine):
    """Data from C3, row 4 and column E hidden, a merge, fills, a number format, a formula, odd sheets."""

    def build(eng):
        import uno

        new = client.props(Hidden=True)
        doc = eng.desktop.loadComponentFromURL("private:factory/scalc", "_blank", 0, new)
        try:
            sheets = doc.getSheets()
            s = sheets.getByIndex(0)
            s.getCellByPosition(2, 2).setString("C3 text")
            s.getCellByPosition(3, 2).setValue(1234.5)
            locale = uno.createUnoStruct("com.sun.star.lang.Locale")
            s.getCellByPosition(3, 2).NumberFormat = doc.getNumberFormats().addNew('#,##0.00" EUR"', locale)
            s.getCellByPosition(2, 3).setString("hidden row")
            s.getRows().getByIndex(3).IsVisible = False
            s.getCellByPosition(4, 4).setString("hidden column")
            s.getColumns().getByIndex(4).IsVisible = False
            s.getCellByPosition(5, 4).setFormula("=D3*2")
            s.getCellByPosition(2, 5).setString("merged")
            s.getCellRangeByName("C6:D7").merge(True)
            s.getCellByPosition(5, 6).setString("filled")
            s.getCellByPosition(5, 6).CellBackColor = 0xFFCC00
            s.getCellByPosition(6, 7).setString("white")
            s.getCellByPosition(6, 7).CellBackColor = 0xFFFFFF
            s.getColumns().getByIndex(5).Width = 4000
            sheets.insertNewByName("Hidden", 1)
            sheets.getByName("Hidden").getCellByPosition(0, 0).setString("secret")
            sheets.getByName("Hidden").IsVisible = False
            sheets.insertNewByName("Empty", 2)
            sheets.insertNewByName("Third", 3)
            sheets.getByName("Third").getCellByPosition(1, 1).setString("B2")
            return client.export(eng, doc, "Calc Office Open XML")
        finally:
            client.close_document(doc)

    return engine.run(build)


def test_tabs_and_displayed_text(engine, fixture_file):
    book = workbook(fixture_file("budget.xlsx"), engine=engine)
    assert book.sheets == ["Summary", "Detail", "Notes"]
    grid = book.grid(0)
    assert grid.cells[(0, 0)].text == "Item" and grid.cells[(1, 2)].text == "4200"


def test_grid_uses_real_sheet_coordinates(engine, layout_xlsx):
    book = workbook(layout_xlsx, engine=engine)
    assert book.sheets == ["Sheet1", "Empty", "Third"]  # the hidden sheet has no tab
    grid = book.grid(0)
    assert grid.rows == [2, 4, 5, 6, 7] and grid.cols == [2, 3, 5, 6]  # row 4 and column E hidden
    assert grid.cells[(2, 2)].text == "C3 text"
    assert grid.cells[(2, 3)].text == "1,234.50 EUR"  # the file's own number format (4.3)
    assert (grid.cells[(5, 2)].rowspan, grid.cells[(5, 2)].colspan) == (2, 2)
    assert grid.cells[(6, 5)].fill == "#FFCC00"
    assert grid.cells[(7, 6)].fill is None  # white is no fill
    assert grid.formulas == {(4, 5): "=D3*2"}
    assert grid.col_widths[2] > grid.col_widths[0] and len(grid.row_heights) == len(grid.rows)
    assert book.grid(1).cells == {} and book.grid(1).rows == []
    assert book.grid(2).cells[(1, 1)].text == "B2"


def test_stored_values_are_not_recalculated(engine):
    data = stale_formula_xlsx()
    grid = workbook(data, engine=engine).grid(0)
    assert grid.cells[(0, 1)].text == "999"
    assert grid.formulas == {(0, 1): "=A1*10"}
    assert formula(data, "Sheet1", 0, 1, engine=engine) == "=A1*10"
    assert formula(data, 0, 0, 0, engine=engine) is None


@pytest.mark.parametrize("name, cell, text", [
    ("budget.csv", (1, 2), "4200"), ("utf8.csv", (1, 0), "Флаг"), ("latin1.csv", (1, 0), "Café"),
])
def test_csv_files(engine, fixture_file, name, cell, text):
    assert workbook(fixture_file(name), engine=engine).grid(0).cells[cell].text == text


def test_russian_csv_from_excel(engine):
    data = "Название;Сумма\r\nЯблоко;12\r\nГруша;7\r\n".encode("cp1251")
    grid = workbook(data, engine=engine).grid(0)
    assert grid.cells[(0, 1)].text == "Сумма" and grid.cells[(2, 0)].text == "Груша"



def test_semicolon_csv_with_decimal_commas(engine):
    """Excel in Russian (and most of Europe) writes ';' between fields and ',' in numbers."""
    data = "Город;Население;Площадь, кв. км\r\nГород 1;1000;3,5\r\nГород 2;2000;7,0\r\n".encode("cp1251")
    grid = workbook(data, engine=engine).grid(0)
    assert grid.cells[(0, 2)].text == "Площадь, кв. км" and grid.cells[(2, 0)].text == "Город 2"
    assert len(grid.cols) == 3

def test_not_a_spreadsheet(engine, fixture_file):
    with pytest.raises(ConversionError):
        workbook(fixture_file("report.docx"), engine=engine)


@pytest.mark.parametrize("name", SPIKE_COLOURED)
def test_spike_cell_colours_survive(engine, corpus_file, name):
    book = workbook(corpus_file(name), engine=engine)
    assert any(cell.fill for i in range(len(book.sheets)) for cell in book.grid(i).cells.values())


def test_cell_notes_and_line_breaks():
    parser = _TableParser()
    parser.feed('<table><colgroup span="2" width="80"></colgroup><tr><td height="20">one<br>two'
                '<a class="comment-indicator"></a><comment><p>a note</p></comment></td>'
                '<td bgcolor="#ffffff"><br></td></tr></table>')
    assert parser.rows[0][0]["text"] == "one\ntwo"
    assert parser.rows[0][1] == {"fill": None, "rowspan": 1, "colspan": 1, "text": ""}
    assert parser.widths == [80, 80] and parser.heights == [20]
