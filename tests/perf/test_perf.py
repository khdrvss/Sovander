"""Time to first page (NFR Performance), cold and warm, reported to build/perf/report.json.

- Word: a 20-page .docx through the engine, page 1 exported and drawn by MuPDF. Cold = engine start
  plus that (the app's own start is not in it yet: there is no app shell before task 5.1). Warm =
  the engine already running, best of two. The synthetic file always runs; the spike's 20-page
  drawing.docx runs too when the corpus is there.
- PDF: the 756-page PDF 32000-2008 (stricter than the NFR's 500 pages): open, every page size (the
  page view lays out all pages), page 1 drawn. Cold = a fresh Python process; warm = in process.

Targets are the NFR's; CI asserts only the ceilings (see conftest.py).
"""

from __future__ import annotations

import io
import json
import os
import subprocess
import sys
import time
import zipfile

import pytest

from sovander.engine import client
from tests.fidelity.engine_harness import ROOT, running_engine, unavailable

try:
    import pymupdf
except ImportError:  # Ubuntu 22.04's python3-fitz only has the old name
    import fitz as pymupdf

REFERENCE = os.environ.get("SOVANDER_PERF_REFERENCE") == "1"
CORPUS = ROOT / "spike" / "corpus"
# name: (NFR target on the reference machine, ceiling on a shared CI runner), seconds
LIMITS = {
    "word_cold_first_page": (5.0, 120.0),
    "word_warm_first_page": (2.0, 30.0),
    "pdf_first_page": (1.5, 15.0),
}


def limit(name: str) -> float:
    target, ceiling = LIMITS[name]
    return target if REFERENCE else ceiling


def check(results: dict, name: str, seconds: float) -> None:
    results[name] = {"seconds": round(seconds, 3), "limit": limit(name), "target": LIMITS[name][0],
                     "meets_target": seconds <= LIMITS[name][0]}
    assert seconds <= limit(name), f"{name}: {seconds:.2f} s, limit {limit(name)} s"


# PDF


def draw_first_page(path) -> dict:
    """Open, read every page size, draw page 1 at 96 dpi: what the page view does for a new PDF."""
    t0 = time.perf_counter()
    doc = pymupdf.open(path)
    t1 = time.perf_counter()
    sizes = [page.rect for page in doc]
    t2 = time.perf_counter()
    doc[0].get_pixmap(matrix=pymupdf.Matrix(96 / 72, 96 / 72))
    t3 = time.perf_counter()
    pages = doc.page_count
    doc.close()
    assert len(sizes) == pages
    return {"pages": pages, "open_s": t1 - t0, "layout_s": t2 - t1, "render_s": t3 - t2, "total_s": t3 - t0}


COLD_PDF = """
import json, sys, time
t0 = time.perf_counter()
try:
    import pymupdf
except ImportError:
    import fitz as pymupdf
t1 = time.perf_counter()
doc = pymupdf.open(sys.argv[1])
[page.rect for page in doc]
doc[0].get_pixmap(matrix=pymupdf.Matrix(96 / 72, 96 / 72))
t2 = time.perf_counter()
print(json.dumps({"import_s": t1 - t0, "first_page_s": t2 - t1}))
"""


def test_pdf_first_page(perf_report):
    path = CORPUS / "pdf" / "pdf32000-2008.pdf"
    if not path.is_file():
        unavailable("spike/corpus/pdf/pdf32000-2008.pdf missing: run scripts/fetch-corpus.sh",
                    "SOVANDER_REQUIRE_CORPUS")
    start = time.perf_counter()
    # not -I: PyMuPDF may come from the user's site-packages on a development machine
    out = subprocess.run([sys.executable, "-B", "-c", COLD_PDF, str(path)], capture_output=True,
                         text=True, timeout=300, check=True)
    cold = json.loads(out.stdout)
    cold["process_s"] = time.perf_counter() - start
    warm = min((draw_first_page(path) for _ in range(3)), key=lambda r: r["total_s"])
    perf_report["pdf_756_pages"] = {"cold": cold, "warm": warm, "pymupdf": pymupdf.VersionBind}
    check(perf_report, "pdf_first_page", max(cold["first_page_s"], warm["total_s"]))


# Word


def twenty_pages_docx() -> bytes:
    """A 20-page report: per page a heading, eight paragraphs and a small table, then a page break."""
    ns = 'xmlns:w="http://schemas.openxmlformats.org/wordprocessingml/2006/main"'
    vocabulary = ("survey", "budget", "quarter", "tenant", "drainage", "report", "inspection", "roof",
                  "schedule", "estimate", "contractor", "garden", "boundary", "invoice", "council", "window")
    body = []
    for page in range(1, 21):
        body.append(f'<w:p><w:r><w:rPr><w:b/><w:sz w:val="32"/></w:rPr><w:t>Section {page}</w:t></w:r></w:p>')
        for para in range(8):
            text = " ".join(vocabulary[(page * 7 + para * 3 + i) % len(vocabulary)] for i in range(40))
            body.append(f'<w:p><w:r><w:t xml:space="preserve">{text.capitalize()}.</w:t></w:r></w:p>')
        rows = "".join("<w:tr>" + "".join(f"<w:tc><w:p><w:r><w:t>{r}.{c}</w:t></w:r></w:p></w:tc>"
                                          for c in range(4)) + "</w:tr>" for r in range(3))
        body.append(f"<w:tbl><w:tblPr><w:tblW w:w=\"5000\" w:type=\"pct\"/></w:tblPr>{rows}</w:tbl>")
        if page < 20:
            body.append('<w:p><w:r><w:br w:type="page"/></w:r></w:p>')
    parts = {
        "[Content_Types].xml": '<Types xmlns="http://schemas.openxmlformats.org/package/2006/content-types">'
        '<Default Extension="rels" ContentType="application/vnd.openxmlformats-package.relationships+xml"/>'
        '<Default Extension="xml" ContentType="application/xml"/><Override PartName="/word/document.xml" '
        'ContentType="application/vnd.openxmlformats-officedocument.wordprocessingml.document.main+xml"/></Types>',
        "_rels/.rels": '<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships">'
        '<Relationship Id="rId1" Target="word/document.xml" Type="http://schemas.openxmlformats.org/'
        'officeDocument/2006/relationships/officeDocument"/></Relationships>',
        "word/document.xml": f'<w:document {ns}><w:body>{"".join(body)}</w:body></w:document>',
    }
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as z:
        for name, text in parts.items():
            z.writestr(name, text)
    return buf.getvalue()


def first_page_seconds(engine, data: bytes) -> float:
    """Page 1 as PDF from the engine, drawn by MuPDF; the rest of the export is not waited for."""
    start = time.perf_counter()
    chunks = client.convert(data, client.TEXT, engine=engine)
    page1 = next(chunks)
    doc = pymupdf.open(stream=bytes(page1), filetype="pdf")
    doc[0].get_pixmap(matrix=pymupdf.Matrix(96 / 72, 96 / 72))
    seconds = time.perf_counter() - start
    doc.close()
    chunks.close()  # closes the document in the engine
    return seconds


def page_count(engine, data: bytes) -> int:
    pdf = list(client.convert(data, client.TEXT, engine=engine))[-1]
    doc = pymupdf.open(stream=bytes(pdf), filetype="pdf")
    try:
        return doc.page_count
    finally:
        doc.close()


@pytest.mark.engine
def test_word_first_page(perf_report):
    docs = {"synthetic-20-pages.docx": twenty_pages_docx()}
    spike = CORPUS / "docx" / "drawing.docx"
    if spike.is_file():
        docs["drawing.docx"] = spike.read_bytes()
    with running_engine("perf", start=False) as engine:
        profile_existed = (engine.profile / "user").is_dir()
        start = time.perf_counter()
        engine.start()
        started = time.perf_counter() - start
        cold = first_page_seconds(engine, docs["synthetic-20-pages.docx"]) + started
        warm = {name: min(first_page_seconds(engine, data) for _ in range(2)) for name, data in docs.items()}
        pages = {name: page_count(engine, data) for name, data in docs.items()}
        version = engine.version
    perf_report["word_20_pages"] = {
        "engine": version, "engine_start_s": round(started, 3), "profile_existed": profile_existed,
        "cold_first_page_s": round(cold, 3), "warm_first_page_s": {k: round(v, 3) for k, v in warm.items()},
        "pages": pages,
    }
    assert pages["synthetic-20-pages.docx"] == 20, pages
    check(perf_report, "word_cold_first_page", cold)
    check(perf_report, "word_warm_first_page", max(warm.values()))
