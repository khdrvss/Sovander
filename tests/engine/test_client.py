import socket
import threading
import time
from pathlib import Path

import pytest

from sovander.engine import client
from sovander.engine.client import (
    SHEET,
    SLIDES,
    TEXT,
    DamagedFile,
    NeedsPassword,
    PdfChunk,
    WrongPassword,
    convert,
)

try:
    import pymupdf
except ImportError:  # Ubuntu 22.04's python3-fitz only has the old name
    import fitz as pymupdf

pytestmark = pytest.mark.engine

FIXTURES = Path(__file__).resolve().parents[1] / "fixtures"
TEST_PHRASE = "sovander-test-phrase"  # throwaway key for the file this test encrypts itself
NS = ('xmlns:office="urn:oasis:names:tc:opendocument:xmlns:office:1.0" '
      'xmlns:text="urn:oasis:names:tc:opendocument:xmlns:text:1.0" '
      'xmlns:style="urn:oasis:names:tc:opendocument:xmlns:style:1.0" '
      'xmlns:fo="urn:oasis:names:tc:opendocument:xmlns:xsl-fo-compatible:1.0" '
      'xmlns:draw="urn:oasis:names:tc:opendocument:xmlns:drawing:1.0" '
      'xmlns:svg="urn:oasis:names:tc:opendocument:xmlns:svg-compatible:1.0" '
      'xmlns:xlink="http://www.w3.org/1999/xlink"')


def flat_odt(body: str) -> bytes:
    return (f'<?xml version="1.0" encoding="UTF-8"?><office:document {NS} office:version="1.3" '
            'office:mimetype="application/vnd.oasis.opendocument.text"><office:automatic-styles>'
            '<style:style style:name="Break" style:family="paragraph">'
            '<style:paragraph-properties fo:break-before="page"/></style:style></office:automatic-styles>'
            f"<office:body><office:text>{body}</office:text></office:body></office:document>").encode()


THREE_PAGES = flat_odt('<text:p>One</text:p><text:p text:style-name="Break">Two</text:p>'
                       '<text:p text:style-name="Break">Three</text:p>')


def pages(pdf: bytes) -> int:
    return pymupdf.open(stream=bytes(pdf), filetype="pdf").page_count  # Ubuntu's PyMuPDF takes no subclass


def first_page_text(pdf: bytes) -> str:
    with pymupdf.open(stream=bytes(pdf), filetype="pdf") as doc:  # Ubuntu's PyMuPDF: no subclass, no
        return doc[0].get_text()  # page outliving its document


def test_page_one_comes_first_then_the_whole_document(engine):
    chunks = list(convert(THREE_PAGES, TEXT, engine=engine))
    assert [(pages(c), c.final) for c in chunks] == [(1, False), (3, True)]
    assert all(isinstance(c, PdfChunk) and c.startswith(b"%PDF") and not c.damaged for c in chunks)
    assert "One" in first_page_text(chunks[0])


def test_one_page_document_is_exported_once(engine, fixture_file):
    chunks = list(convert(fixture_file("report.docx"), TEXT, engine=engine))
    assert len(chunks) == 1 and chunks[0].final and pages(chunks[0]) == 1


@pytest.mark.parametrize("name, kind", [
    ("letter.odt", TEXT), ("memo.rtf", TEXT), ("legacy.doc", TEXT), ("report.docm", TEXT),
    ("report.dotx", TEXT), ("deck.pptx", SLIDES), ("deck.ppsx", SLIDES), ("deck.potx", SLIDES),
    ("budget.xlsx", SHEET), ("budget.ods", SHEET), ("budget.xls", SHEET),
])
def test_formats_convert(engine, fixture_file, name, kind):
    final = list(convert(fixture_file(name), kind, engine=engine))[-1]
    assert final.final and pages(final) >= 1


def test_content_wins_over_the_given_kind(engine, fixture_file):
    deck = fixture_file("deck.pptx").read_bytes()
    as_slides = list(convert(deck, SLIDES, engine=engine))[-1]
    as_text = list(convert(deck, TEXT, engine=engine))[-1]
    assert pages(as_text) == pages(as_slides)


def test_a_comment_leaves_the_engine_running(engine):
    """Writer builds a comment's window from annotation.ui (libreoffice-uiconfig-writer, which 24.04's
    libreoffice-writer-nogui does not pull); without the file soffice aborts."""
    doc = flat_odt('<text:p>Commented<office:annotation><text:p>A note</text:p></office:annotation></text:p>')
    assert "Commented" in first_page_text(list(convert(doc, TEXT, engine=engine))[-1])


@pytest.fixture(scope="module")
def encrypted_odt(engine):
    def make(eng):
        doc, _ = client.open_document(eng, (FIXTURES / "letter.odt").read_bytes(), TEXT)
        try:
            out = eng.create("com.sun.star.io.SequenceOutputStream")
            args = {"FilterName": "writer8", "OutputStream": out, "Password": TEST_PHRASE}
            doc.storeToURL("private:stream", client.props(**args))
            return out.getWrittenBytes().value
        finally:
            client.close_document(doc)

    return engine.run(make)


def test_password_needed_wrong_and_right(engine, encrypted_odt):
    with pytest.raises(NeedsPassword) as needed:
        list(convert(encrypted_odt, TEXT, engine=engine))
    assert not isinstance(needed.value, WrongPassword)
    with pytest.raises(WrongPassword):
        list(convert(encrypted_odt, TEXT, password=TEST_PHRASE + "-typo", engine=engine))
    assert pages(list(convert(encrypted_odt, TEXT, password=TEST_PHRASE, engine=engine))[-1]) >= 1


def test_encrypted_word_file_from_the_corpus(engine, corpus_file):
    path = corpus_file("docx/bug53475-password-is-solrcell.docx")
    phrase = path.stem.rsplit("-", 1)[-1]  # Apache POI names the file after its password
    with pytest.raises(NeedsPassword):
        list(convert(path, TEXT, engine=engine))
    assert pages(list(convert(path, TEXT, password=phrase, engine=engine))[-1]) >= 1


def test_damaged_zip_is_repaired_and_flagged(engine, fixture_file, repairs_ooxml):
    data = fixture_file("report.docx").read_bytes()
    chunks = list(convert(data[: int(len(data) * 0.9)], TEXT, engine=engine))
    assert chunks[-1].damaged and pages(chunks[-1]) >= 1


def test_unreadable_file_is_reported_damaged(engine):
    with pytest.raises(DamagedFile):
        list(convert(b"PK\x03\x04" + b"garbage" * 100, TEXT, engine=engine))


def test_remote_image_is_never_fetched(engine):
    """Requirement 9.2: a linked web image stays a placeholder; nothing connects to its host."""
    listener = socket.socket()
    listener.bind(("127.0.0.1", 0))
    listener.listen(5)
    listener.settimeout(0.2)
    port = listener.getsockname()[1]
    hits = []
    done = threading.Event()

    def accept():
        while not done.is_set():
            try:
                conn, _ = listener.accept()
            except OSError:
                continue
            hits.append(conn.recv(100))
            conn.close()

    thread = threading.Thread(target=accept, daemon=True)
    thread.start()
    doc = flat_odt('<text:p>Remote<draw:frame draw:name="i" svg:width="2cm" svg:height="2cm" '
                   f'text:anchor-type="as-char"><draw:image xlink:href="http://127.0.0.1:{port}/canary.png" '
                   'xlink:type="simple" xlink:show="embed" xlink:actuate="onLoad"/></draw:frame></text:p>')
    try:
        final = list(convert(doc, TEXT, engine=engine))[-1]
        time.sleep(1)
    finally:
        done.set()
        thread.join(2)
        listener.close()
    assert "Remote" in first_page_text(final)
    assert hits == []


def test_unknown_kind_is_rejected():
    with pytest.raises(ValueError):
        list(convert(b"x", "video"))
