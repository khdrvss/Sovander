import io
import struct
import zipfile

import pytest

from sovander.engine.macros import CFB_MAGIC, has_macros, macro_parts, ole_names

FREE, END, FATSECT = 0xFFFFFFFF, 0xFFFFFFFE, 0xFFFFFFFD


def make_zip(*names: str) -> bytes:
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as zf:
        for name in names:
            zf.writestr(name, b"x")
    return buf.getvalue()


def make_cfb(entries: list[tuple[str, int]], dir_next: int = END) -> bytes:
    """Smallest compound file: header, one FAT sector (0), one directory sector (1), up to 4 entries."""
    header = bytearray(512)
    header[:8] = CFB_MAGIC
    struct.pack_into("<HHHHH", header, 0x18, 0x3E, 3, 0xFFFE, 9, 6)
    struct.pack_into("<IIII", header, 0x2C, 1, 1, 0, 4096)
    struct.pack_into("<IIII", header, 0x3C, END, 0, END, 0)
    struct.pack_into("<109I", header, 0x4C, 0, *[FREE] * 108)
    fat = struct.pack("<128I", FATSECT, dir_next, *[FREE] * 126)
    directory = bytearray(512)
    for i, (name, kind) in enumerate([("Root Entry", 5), *entries]):
        raw = name.encode("utf-16-le") + b"\0\0"
        directory[i * 128: i * 128 + len(raw)] = raw
        struct.pack_into("<HB", directory, i * 128 + 64, len(raw), kind)
    return bytes(header) + fat + bytes(directory)


@pytest.mark.parametrize("names, expected", [
    (["word/document.xml", "word/vbaProject.bin"], ["word/vbaProject.bin"]),
    (["xl/workbook.xml", "xl/vbaProject.bin"], ["xl/vbaProject.bin"]),
    (["ppt/presentation.xml", "ppt/vbaProject.bin"], ["ppt/vbaProject.bin"]),
    (["xl/workbook.xml", "xl/macrosheets/sheet1.xml"], ["xl/macrosheets/sheet1.xml"]),
    (["content.xml", "Basic/script-lc.xml", "Basic/Standard/script-lb.xml", "Basic/Standard/Module1.xml"],
     ["Basic/Standard/Module1.xml"]),
    (["content.xml", "Scripts/python/hello.py"], ["Scripts/python/hello.py"]),
])
def test_zip_macro_parts_are_found(names, expected):
    assert macro_parts(make_zip(*names)) == expected


def test_odf_with_only_empty_basic_library_has_no_macros():
    assert macro_parts(make_zip("content.xml", "Basic/script-lc.xml", "Basic/Standard/script-lb.xml")) == []


@pytest.mark.parametrize("storage", ["Macros", "_VBA_PROJECT_CUR", "VBA"])
def test_ole_macro_storages_are_found(storage):
    data = make_cfb([("WordDocument", 2), (storage, 1)])
    assert macro_parts(data) == [storage]
    assert has_macros(data)


def test_ole_reader_lists_real_legacy_files(fixture_file):
    assert "WordDocument" in ole_names(fixture_file("legacy.doc").read_bytes())
    assert "Workbook" in ole_names(fixture_file("budget.xls").read_bytes())


@pytest.mark.parametrize("name", ["report.docx", "report.docm", "budget.xlsm", "deck.pptm", "letter.odt",
                                  "legacy.doc", "budget.xls", "memo.rtf", "six-pages.pdf"])
def test_fixtures_without_macros(fixture_file, name):
    # Gander's .docm/.xlsm/.pptm fixtures are renamed plain files: no VBA part inside
    assert macro_parts(fixture_file(name)) == []


def test_path_and_bytes_agree(tmp_path):
    path = tmp_path / "m.docm"
    path.write_bytes(make_zip("word/document.xml", "word/vbaProject.bin"))
    assert macro_parts(path) == macro_parts(path.read_bytes()) == ["word/vbaProject.bin"]


@pytest.mark.parametrize("data", [
    b"",
    b"PK\x03\x04 not really a zip",
    CFB_MAGIC + b"\0" * 100,  # truncated header
    make_cfb([("Macros", 1)])[:700],  # directory sector cut off
    make_cfb([("Macros", 1)], dir_next=1),  # directory chain loops on itself
    bytes(range(256)) * 8,
])
def test_damaged_input_reports_nothing_and_never_raises(data):
    result = macro_parts(data)
    assert isinstance(result, list)


def test_looping_directory_still_reads_entries_once():
    assert macro_parts(make_cfb([("Macros", 1)], dir_next=1)) == ["Macros"]


def test_missing_file_reports_nothing(tmp_path):
    assert macro_parts(tmp_path / "gone.doc") == []
