"""Fast previews of .csv/.tsv and .ods/.fods (task C7): values as stored, bounded work, never a crash."""

import io
import time
import zipfile

import pytest

from sovander.engine.client import csv_encoding, csv_separator
from sovander.sheets_fast import first_screen, text_preview
from sovander.sheets_fast_odf import preview as ods_preview

MIME = "application/vnd.oasis.opendocument.spreadsheet"
NS = ('xmlns:office="urn:oasis:names:tc:opendocument:xmlns:office:1.0" '
      'xmlns:style="urn:oasis:names:tc:opendocument:xmlns:style:1.0" '
      'xmlns:table="urn:oasis:names:tc:opendocument:xmlns:table:1.0" '
      'xmlns:text="urn:oasis:names:tc:opendocument:xmlns:text:1.0" xmlns:dc="http://purl.org/dc/elements/1.1/"')
STYLES = ('<office:automatic-styles><style:style style:name="ta1" style:family="table">'
          '<style:table-properties table:display="true"/></style:style><style:style style:name="ta2" '
          'style:family="table"><style:table-properties table:display="false"/></style:style>'
          '</office:automatic-styles>')
DATA = ('<table:table table:name="Secret" table:style-name="ta2"><table:table-row><table:table-cell '
        'office:value-type="string"><text:p>hidden</text:p></table:table-cell></table:table-row></table:table>'
        '<table:table table:name="Data" table:style-name="ta1">'
        '<table:table-column table:number-columns-repeated="2"/>'
        '<table:table-column table:visibility="collapse"/>'
        '<table:table-column table:number-columns-repeated="16381"/><table:table-row>'
        '<table:table-cell office:value-type="string"><text:p>Привет</text:p><text:p>мир</text:p>'
        '</table:table-cell>'
        '<table:table-cell office:value-type="float" office:value="1234.5"><text:p>1 234,50</text:p>'
        '</table:table-cell><table:table-cell office:value-type="float" office:value="7"><text:p>7</text:p>'
        '</table:table-cell><table:table-cell office:value-type="float" office:value="0.30000000000000004"/>'
        '<table:table-cell office:value-type="date" office:date-value="2026-10-17T00:00:00"/>'
        '<table:table-cell office:value-type="boolean" office:boolean-value="true"/>'
        '<table:table-cell><office:annotation><dc:creator>me</dc:creator><text:p>note</text:p></office:annotation>'
        '<text:p>a<text:s text:c="2"/>b</text:p></table:table-cell>'
        '<table:table-cell office:value-type="float" office:value="5" table:number-columns-repeated="3">'
        '<text:p>5</text:p></table:table-cell><table:table-cell table:number-columns-repeated="16373"/>'
        '</table:table-row><table:table-row table:visibility="collapse"><table:table-cell>'
        '<text:p>gone</text:p></table:table-cell></table:table-row>'
        '<table:table-row table:number-rows-repeated="2"><table:table-cell>'
        '<text:p>twice</text:p></table:table-cell></table:table-row>'
        '<table:table-row table:number-rows-repeated="1048570"><table:table-cell '
        'table:number-columns-repeated="16384"/></table:table-row><table:table-row><table:table-cell>'
        '<text:p>far</text:p></table:table-cell></table:table-row></table:table>')


def content(tables: str, root: str = "document-content", extra: str = "") -> bytes:
    return (f'<?xml version="1.0" encoding="UTF-8"?><office:{root} {NS}{extra}>{STYLES}<office:body>'
            f'<office:spreadsheet>{tables}</office:spreadsheet></office:body></office:{root}>').encode()


def ods(tables: str) -> bytes:
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as zf:
        zf.writestr("mimetype", MIME, compress_type=zipfile.ZIP_STORED)
        zf.writestr("content.xml", content(tables), compress_type=zipfile.ZIP_DEFLATED)
    return buf.getvalue()


def fods(tables: str) -> bytes:
    return content(tables, "document", f' office:mimetype="{MIME}"')


def table(name: str, rows: int, cols: int = 3) -> str:
    def cell(n: int) -> str:
        return (f'<table:table-cell office:value-type="float" office:value="{n}">'
                f"<text:p>{n}</text:p></table:table-cell>")

    row = "<table:table-row>{}</table:table-row>"
    body = "".join(row.format("".join(cell(r * 100 + c) for c in range(cols))) for r in range(rows))
    return f'<table:table table:name="{name}">{body}</table:table>'


def texts(grid):
    return {key: cell.text for key, cell in grid.cells.items()}


def test_ods_values_repeats_and_hidden_parts():
    for data in (ods(DATA), fods(DATA)):
        book = first_screen(data, "book.ods")
        assert book.provisional and book.sheets == ["Data"]  # the hidden sheet has no tab
        grid = book.grid(0)
        assert grid.rows == [0, 2, 3]  # hidden row 1 left out; "far" lies past the 1,048,570 empty rows
        assert grid.cols == [0, 1, 3, 4, 5, 6, 7, 8, 9]  # hidden column 2 left out
        text = texts(grid)
        assert text[(0, 0)] == "Привет\nмир" and text[(0, 1)] == "1 234,50"  # the text the file shows
        assert (text[(0, 3)], text[(0, 4)], text[(0, 5)]) == ("0.3", "2026-10-17", "TRUE")  # raw values
        assert text[(0, 6)] == "a  b"  # the comment is not the cell's text
        assert [text[(0, c)] for c in (7, 8, 9)] == ["5", "5", "5"]
        assert text[(2, 0)] == text[(3, 0)] == "twice"


def test_ods_skips_the_rest_of_a_full_table_and_finds_the_next():
    tables = table("A", 50) + '<table:table table:name="B" table:style-name="ta2"/>' + table("C", 2)
    for data in (ods(tables), fods(tables)):
        book = ods_preview(data, max_rows=10)
        assert book.sheets == ["A", "C"]
        assert book.grid(0).rows == list(range(10)) and texts(book.grid(1))[(1, 2)] == "102"


def test_large_ods_preview_is_fast():
    data = ods(table("Big", 30_000, 20) + table("Tail", 1))
    start = time.perf_counter()
    book = ods_preview(data)
    took = time.perf_counter() - start
    assert book.sheets == ["Big", "Tail"] and len(book.grid(0).rows) == 1000 and len(book.grid(0).cols) == 20
    assert took < 1.0, took


def test_csv_separator_encoding_and_quotes():
    data = '﻿Имя;Сумма;Заметка\n"Иван";1,5;"две\nстроки"\n;;\nПётр;2;\n'.encode()
    grid = first_screen(data, "отчёт.csv").grid(0)
    assert first_screen(data, "отчёт.csv").sheets == ["отчёт"]
    assert texts(grid) == {(0, 0): "Имя", (0, 1): "Сумма", (0, 2): "Заметка", (1, 0): "Иван", (1, 1): "1,5",
                           (1, 2): "две\nстроки", (3, 0): "Пётр", (3, 1): "2"}
    assert grid.rows == [0, 1, 2, 3] and grid.cols == [0, 1, 2]


def test_csv_cp1251_tsv_and_bad_bytes():
    cp1251 = "Имя,Город\nИван,Москва\n".encode("cp1251")
    assert texts(text_preview(cp1251, "a.csv").grid(0))[(1, 1)] == "Москва"
    assert texts(text_preview(b"a,b\tc\n1,2\t3\n", "a.tsv").grid(0)) == {
        (0, 0): "a,b", (0, 1): "c", (1, 0): "1,2", (1, 1): "3"}
    for data in (b"", b"\x00\x01\xff,\x98\n", b"one column\nonly\n", b"x" * 70_000 + b",\xff\n\xc3,1"):
        assert text_preview(data, "bad.csv").sheets == ["bad"]  # never raises (bad bytes past 64 KB: U+FFFD)


def test_150k_rows_csv_preview_is_fast():
    data = "".join(",".join(str(r * 20 + c) for c in range(20)) + "\n" for r in range(150_000)).encode()
    start = time.perf_counter()
    grid = first_screen(data, "big.csv").grid(0)
    took = time.perf_counter() - start
    assert len(grid.rows) == 1000 and len(grid.cols) == 20
    assert grid.cells[(999, 19)].text == str(999 * 20 + 19)
    assert took < 1.0, took


def test_crafted_ods_cell_text_stays_bounded():
    cell = ('<table:table-cell><text:p>a<text:s text:c="2000000000"/>b</text:p>'
            f'<text:p>{"y" * 40_000}</text:p></table:table-cell>')
    grid = ods_preview(ods(f'<table:table table:name="T"><table:table-row>{cell}</table:table-row>'
                           '</table:table>')).grid(0)
    text = grid.cells[(0, 0)].text
    assert len(text) == 32767 and text.startswith("a ")


@pytest.mark.parametrize(("text", "separator"), [
    ("Город;Население;Площадь, кв. км\r\nГород 1;1000;3,5\r\nГород 2;2000;7,0\r\n", ";"),  # Excel, Russian
    ("a;b\n1,5;2,5\n3,5;4,5\n", ";"),  # decimal commas outnumber the fields
    ("name,price\nApple,1.5\nPear,2\n", ","),
    ('name,note\nAnn,"Hello, world"\nBob,"Hi, there"\n', ","),  # quoted commas are not fields
    ("Hello, world, again\t5\nGood, bye, now\t6\n", "\t"),  # a tab wins over prose commas
    ("Hello, world;5\nGood, bye;6\n", ";"),
    ("a|b|c\n1|2|3\n", "|"),
    ("one column\nonly\n", ","),
])
def test_csv_separator(text, separator):
    assert csv_separator(text) == separator


@pytest.mark.parametrize(("data", "encoding"), [
    ("Имя;Город\r\nИван;Москва\r\n".encode("utf-16"), "utf-16"),  # Excel's "Unicode text"
    ("Имя;Город\nИван;Москва\n".encode(), "utf-8"),
    ("Имя;Город\r\nИван;Москва\r\n".encode("cp1251"), "cp1251"),
    ("Name;Town\r\nCafé;Zürich\r\n".encode("cp1252"), "cp1252"),
])
def test_csv_encoding_and_the_preview_agree_with_the_engine(data, encoding):
    assert csv_encoding(data) == encoding
    grid = text_preview(data, "a.csv").grid(0)
    assert grid.cells[(1, 1)].text in ("Москва", "Zürich") and len(grid.cols) == 2


def test_russian_excel_csv_preview_splits_at_semicolons():
    data = "Город;Население;Площадь, кв. км\r\nГород 1;1000;3,5\r\n".encode("cp1251")
    assert texts(text_preview(data, "goroda.csv").grid(0)) == {
        (0, 0): "Город", (0, 1): "Население", (0, 2): "Площадь, кв. км",
        (1, 0): "Город 1", (1, 1): "1000", (1, 2): "3,5"}
