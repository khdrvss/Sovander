"""Routing by content (Requirements 2, 2.11, 7): every Gander fixture, and misnamed copies."""

import io
import struct
import zipfile
from pathlib import Path

import pytest

from sovander.router import (
    HEAD_BYTES,
    Kind,
    detect,
    detect_bytes,
    detect_path,
    is_known_text_name,
    reads_as_text,
)

FIXTURES = Path(__file__).resolve().parents[1] / "fixtures"

K = Kind
ROUTES = {
    "GANDER-README.md": K.MARKDOWN, "notes.md": K.MARKDOWN,
    **dict.fromkeys([
        "ccitt.pdf", "cjk.pdf", "colours.pdf", "dense-map.pdf", "embedded-font.pdf", "encrypted.pdf",
        "forty-pages.pdf", "jbig2.pdf", "jpx.pdf", "mixed-width.pdf", "ragged-prose.pdf", "six-pages.pdf",
    ], K.PDF),
    **dict.fromkeys([
        "cell-directions.docx", "raised-runs.docx", "report.docm", "report.docx", "report.dotx",
        "word-colours.docx", "word-columns.docx", "word-lines.docx", "word-pages.docx",
        "word-unrecorded.docx",
    ], K.WORD),
    "legacy.doc": K.WORD_LEGACY, "letter.odt": K.ODT, "memo.rtf": K.RTF,
    **dict.fromkeys([
        "budget.csv", "budget.ods", "budget.xls", "budget.xlsb", "budget.xlsm", "budget.xlsx", "budget.xltx",
        "latin1.csv", "utf8.csv",
    ], K.SHEET),
    **dict.fromkeys([
        "charted.pptx", "deck-no-app-xml.pptx", "deck.potx", "deck.ppsx", "deck.pptm", "deck.pptx",
        "freeforms.pptx", "grounds.pptx", "inherited-bold.pptx", "line-breaks.pptx", "lines.pptx",
        "placed-by-design.pptx", "symbol-bullets.pptx", "undrawn.pptx", "unreadable.pptx", "unsized.pptx",
        "unwrapped.pptx", "weights.pptx", "wrapping.pptx",
    ], K.SLIDES),
    **dict.fromkeys(["almanac.epub", "gazetteer.epub", "harbour.epub", "locked.epub"], K.EPUB),
    **dict.fromkeys([
        "anim.gif", "exif-1.jpg", "exif-3.jpg", "exif-5.jpg", "exif-6.jpg", "exif-7.jpg", "exif-8.jpg",
        "icon.svg", "moving.png", "moving.webp", "photo.bmp", "photo.heic", "photo.webp", "still.webp",
        "tiny.png",
    ], K.IMAGE),
    **dict.fromkeys([
        "archive.zip", "deflate64.zip", "locked-cyrillic.zip", "locked.zip", "names-cp866.zip",
        "names-gbk.zip", "names-korean.zip", "names-mac.zip", "names-sjis.zip", "odd-names.zip", "zip64.zip",
    ], K.ZIP),
    # Text by content, whatever the name says: a .pdf that is text, an extension nothing claims
    **dict.fromkeys([
        "bracket-ascii.stl", "captions.srt", "captions.vtt", "not-a-pdf.pdf", "plain.txt", "playlist.m3u",
        "pump.v", "release.nfo", "snapshot.json", "unknown.xyz", "utf16be.txt", "utf16le.txt",
    ], K.TEXT),
    # A binary STL, audio, and bytes of no format at all
    **dict.fromkeys(["bracket.stl", "opaque.xyz", "tone.wav"], K.UNKNOWN_BINARY),
}


def test_every_fixture_has_a_route():
    assert sorted(p.name for p in FIXTURES.iterdir() if p.is_file()) == sorted(ROUTES)


@pytest.mark.parametrize("name", sorted(ROUTES))
def test_every_fixture_routes_by_its_content(name):
    assert detect_path(FIXTURES / name) is ROUTES[name]


@pytest.mark.parametrize("name", sorted(ROUTES))
def test_the_head_alone_routes_every_fixture_the_same(name):
    head = (FIXTURES / name).read_bytes()[:HEAD_BYTES]
    assert detect(head, name) is ROUTES[name]


@pytest.mark.parametrize(("fixture", "named", "kind"), [
    ("six-pages.pdf", "report.txt", K.PDF),
    ("report.docx", "report.zip", K.WORD),
    ("budget.xlsx", "budget.docx", K.SHEET),
    ("deck.pptx", "deck.xlsx", K.SLIDES),
    ("memo.rtf", "memo.doc", K.RTF),  # a .doc is Rich Text about as often as not
    ("legacy.doc", "legacy.xls", K.WORD_LEGACY),
    ("budget.xls", "budget.doc", K.SHEET),
    ("letter.odt", "letter.zip", K.ODT),
    ("budget.ods", "budget.odt", K.SHEET),
    ("harbour.epub", "harbour.zip", K.EPUB),
    ("archive.zip", "archive.docx", K.ZIP),
    ("tiny.png", "tiny.jpg", K.IMAGE),
    ("photo.heic", "photo.txt", K.IMAGE),
    ("icon.svg", "icon.txt", K.IMAGE),
    ("plain.txt", "plain.pdf", K.TEXT),
    ("plain.txt", "plain.docx", K.TEXT),
    ("opaque.xyz", "opaque.txt", K.UNKNOWN_BINARY),
    ("opaque.xyz", "opaque.md", K.UNKNOWN_BINARY),
    # Among text formats the extension decides
    ("notes.md", "notes.txt", K.TEXT),
    ("plain.txt", "plain.md", K.MARKDOWN),
    ("plain.txt", "plain.csv", K.SHEET),
])
def test_content_wins_over_a_wrong_extension(fixture, named, kind):
    assert detect_bytes((FIXTURES / fixture).read_bytes(), named) is kind


def cfb(entries: list[bytes]) -> bytes:
    """A compound file (MS-CFB v3) with up to four directory entries, entry 0 the root."""
    header = b"\xd0\xcf\x11\xe0\xa1\xb1\x1a\xe1" + bytes(16) + struct.pack(
        "<5H6x9I", 0x3E, 3, 0xFFFE, 9, 6, 0, 1, 1, 0, 4096, 0xFFFFFFFE, 0, 0xFFFFFFFE, 0,
    ) + struct.pack("<109I", 0, *[0xFFFFFFFF] * 108)
    fat = struct.pack("<128I", 0xFFFFFFFD, 0xFFFFFFFE, *[0xFFFFFFFF] * 126)
    return header + fat + b"".join(entries).ljust(512, b"\x00")


def entry(name: str, kind: int, right: int = 0xFFFFFFFF, child: int = 0xFFFFFFFF) -> bytes:
    raw = (name + "\x00").encode("utf-16-le")
    fields = struct.pack("<HBB3I", len(raw), kind, 1, 0xFFFFFFFF, right, child)
    return raw.ljust(64, b"\x00") + fields + bytes(36) + struct.pack("<IQ", 0xFFFFFFFE, 0)


def root_with(*streams: str) -> bytes:
    last = len(streams) - 1
    chained = [entry(s, 2, right=i + 2 if i < last else 0xFFFFFFFF) for i, s in enumerate(streams)]
    return cfb([entry("Root Entry", 5, child=1), *chained])


@pytest.mark.parametrize(("streams", "named", "kind"), [
    (["PowerPoint Document", "Current User"], "talk.bin", K.SLIDES),
    (["Workbook"], "data.doc", K.SHEET),
    (["Book"], "old.xls", K.SHEET),
    (["WordDocument", "1Table"], "letter", K.WORD_LEGACY),
    # An encrypted OOXML package says nothing of its family: the name does
    (["EncryptionInfo", "EncryptedPackage"], "secret.docx", K.WORD),
    (["EncryptionInfo", "EncryptedPackage"], "secret.xlsx", K.SHEET),
    (["EncryptionInfo", "EncryptedPackage"], "secret.pptx", K.SLIDES),
    (["EncryptionInfo", "EncryptedPackage"], "secret.bin", K.UNKNOWN_BINARY),
    (["__substg1.0_0037001F"], "mail.msg", K.UNKNOWN_BINARY),
])
def test_ole_files_route_by_their_streams(streams, named, kind):
    assert detect_bytes(root_with(*streams), named) is kind


def test_an_embedded_object_does_not_decide_the_container():
    """A workbook with a Word object in it holds a WordDocument stream one storage down."""
    data = cfb([
        entry("Root Entry", 5, child=1),
        entry("Workbook", 2, right=2),
        entry("MBD0001", 1, child=3),
        entry("WordDocument", 2),
    ])
    assert detect_bytes(data, "budget.xls") is K.SHEET


def ooxml(*names: str, filler: int = 0) -> bytes:
    out = io.BytesIO()
    with zipfile.ZipFile(out, "w") as z:
        z.writestr("[Content_Types].xml", "<Types/>")
        if filler:
            z.writestr("docProps/thumbnail.bin", bytes(range(256)) * (filler // 256), zipfile.ZIP_STORED)
        for name in names:
            z.writestr(name, "<x/>")
    return out.getvalue()


def test_the_zip_index_names_parts_past_the_head():
    data = ooxml("word/document.xml", filler=2 * HEAD_BYTES)
    assert detect_bytes(data, "letter.docx") is K.WORD
    assert detect_bytes(data, "letter.bin") is K.WORD


def test_an_ooxml_package_routes_by_its_part_folders():
    assert detect_bytes(ooxml("xl/workbook.xml"), "a.docx") is K.SHEET
    assert detect_bytes(ooxml("ppt/presentation.xml"), "a") is K.SLIDES
    assert detect_bytes(ooxml("visio/document.xml"), "a.vsdx") is K.ZIP


def test_odf_and_epub_route_by_their_mimetype_member():
    def package(mimetype: str) -> bytes:
        out = io.BytesIO()
        with zipfile.ZipFile(out, "w") as z:
            z.writestr("mimetype", mimetype, zipfile.ZIP_STORED)
            z.writestr("content.xml", "<x/>")
        return out.getvalue()

    oasis = "application/vnd.oasis.opendocument."
    assert detect_bytes(package(oasis + "presentation"), "talk.zip") is K.SLIDES
    assert detect_bytes(package(oasis + "graphics"), "plan") is K.DRAWING
    assert detect_bytes(package(oasis + "text-template"), "a.ott") is K.ODT
    assert detect_bytes(package(oasis + "spreadsheet"), "a.zip") is K.SHEET
    assert detect_bytes(package("application/epub+zip"), "book") is K.EPUB


@pytest.mark.parametrize(("data", "named", "kind"), [
    (b"II*\x00\x08\x00\x00\x00" + bytes(64), "scan", K.IMAGE),
    (b"MM\x00*\x00\x00\x00\x08" + bytes(64), "scan", K.IMAGE),
    (b"\x00\x00\x01\x00\x01\x00\x10\x10" + bytes(64), "favicon", K.IMAGE),
    (struct.pack(">I", 28) + b"ftypavif\x00\x00\x00\x00avifmif1miaf" + bytes(32), "photo", K.IMAGE),
    (struct.pack(">I", 24) + b"ftypisom\x00\x00\x02\x00isomiso2" + bytes(32), "clip.mp4", K.UNKNOWN_BINARY),
    (b"BMW owners' club, minutes of the March meeting\n", "notes", K.TEXT),
    (b'<?xml version="1.0"?>\n<!-- drawn by hand -->\n<!DOCTYPE svg>\n<svg viewBox="0 0 1 1"/>', "a",
     K.IMAGE),
    (b"<html><body><svg/></body></html>", "page.html", K.TEXT),
    (b'<?xml version="1.0"?><office:document office:mimetype="application/vnd.oasis.opendocument.text">',
     "letter.fodt", K.ODT),
    (b"junk before the header\n%PDF-1.7\n", "scan.pdf", K.PDF),
    (b"a note that mentions %PDF-1.7 in passing\n", "note.txt", K.TEXT),
    (b"", "empty.txt", K.TEXT),
    (b"", "empty.md", K.MARKDOWN),
    (b"PK\x05\x06" + bytes(18), "empty.zip", K.ZIP),
])
def test_signatures_beyond_the_fixtures(data, named, kind):
    assert detect_bytes(data, named) is kind


def test_text_is_what_has_no_zero_byte_early_or_opens_with_a_utf16_mark():
    assert reads_as_text(b"plain words\n")
    assert reads_as_text(b"\xff\xfe" + "Привет".encode("utf-16-le"))
    assert reads_as_text(b"x" * 8000 + b"\x00")  # git reads the first 8000 bytes, and so does this
    assert not reads_as_text(b"x" * 7999 + b"\x00")
    assert not reads_as_text(b"\x7fELF\x02\x01\x01\x00")


def test_a_text_view_knows_when_the_name_did_not_say_text():
    assert is_known_text_name("release.nfo")
    assert is_known_text_name("/home/u/notes.MD")
    assert not is_known_text_name("unknown.xyz")
    assert not is_known_text_name(".bashrc")
    assert not is_known_text_name("README")
