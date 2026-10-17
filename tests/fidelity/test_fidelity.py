"""Fidelity floors from the spike (spike/RESULTS.md), so that a regression in the engine path fails.

Grading as in spike/compare.py: the truth is each file's own XML, the render is the text of the PDF
the engine produces (Word, slides) or the fills of its HTML grid (spreadsheets). The file sets are
the spike's own (spike_baseline.json), so the numbers compare like for like. A file that no longer
renders counts as 0% shown.

Floors (spike B, LibreOffice 26.2 snap):
- Word: mean text recall >= 95% (spike 95.8%) and at most 6 of 40 files under 90% (spike: 5).
  The product's engine is Ubuntu's (24.04: LibreOffice 24.2.7): 95.1% and 6 files, the sixth
  being Bug54771b.docx, five words, whose title in a content control (w:sdt) 24.2 leaves out
  and 26.2 shows. The floors guard our engine path against the engine the .deb depends on.
- Slides: mean text recall >= 95% (spike 95.3%, 21 decks).
- Spreadsheets: cell fills shown in 14 of the 14 workbooks the spike saw colours in.

The NFR asks for more than this measures: 98% of the words of a *reference render* over 100 files
per family. The corpus has 61/30/30 files and no Office render to compare with; see tasks.md 6.3.
"""

from __future__ import annotations

import json
import time
from pathlib import Path

import pytest
from corpus_lock import CORPUS, read_lock, sha256
from corpus_lock import present as corpus_present
from engine_harness import required, running_engine, unavailable
from fidelity_truth import recall, truth_words

from sovander.engine import client
from sovander.engine.process import EngineError
from sovander.engine.sheets import workbook

try:
    import pymupdf
except ImportError:  # Ubuntu 22.04's python3-fitz only has the old name
    import fitz as pymupdf

BASELINE = json.loads(Path(__file__).with_name("spike_baseline.json").read_text(encoding="utf-8"))
WORD_MEAN_FLOOR = 0.95
WORD_UNDER_90_MAX = 6  # Ubuntu 24.04's LibreOffice 24.2.7 (26.2: 5)
SLIDES_MEAN_FLOOR = 0.95
COLOURED_FLOOR = len(BASELINE["coloured_workbooks"])  # 14

if not corpus_present():
    if required("SOVANDER_REQUIRE_CORPUS"):
        pytest.fail("spike/corpus is missing: run scripts/fetch-corpus.sh", pytrace=False)
    pytest.skip("spike/corpus is missing: run scripts/fetch-corpus.sh", allow_module_level=True)


@pytest.fixture(scope="module")
def engine():
    with running_engine("fidelity") as eng:
        yield eng


def corpus_path(name: str) -> Path:
    path = CORPUS / name
    if not path.is_file():
        unavailable(f"corpus file {name} missing: run scripts/fetch-corpus.sh", "SOVANDER_REQUIRE_CORPUS")
    return path


def test_corpus_matches_the_lock():
    """Every file the floors grade is the exact file the spike graded."""
    entries = read_lock()
    missing = [e.path for e in entries if not (CORPUS / e.path).is_file()]
    changed = [e.path for e in entries if e.path not in missing and sha256(CORPUS / e.path) != e.sha256]
    assert not missing, f"{len(missing)} corpus files missing (scripts/fetch-corpus.sh): {missing[:5]}"
    assert not changed, f"corpus files differ from corpus.lock: {changed}"


def pdf_text(engine, path: Path, kind: str) -> tuple[str, int]:
    """Text of every page of the engine's PDF (the full export, not just page 1), and the page count."""
    chunks = list(client.convert(path.read_bytes(), kind, engine=engine))
    doc = pymupdf.open(stream=bytes(chunks[-1]), filetype="pdf")
    try:
        return "\n\f".join(page.get_text() for page in doc), doc.page_count
    finally:
        doc.close()


def grade(engine, files: dict[str, float], kind: str) -> list[dict]:
    rows = []
    for name, spike in files.items():
        path = corpus_path(name)
        truth = truth_words(path)
        assert truth, f"{name}: no truth words, but the spike had some"
        start = time.monotonic()
        try:
            text, pages = pdf_text(engine, path, kind)
            row = {"file": name, "status": "ok", "pages": pages, "recall": recall(truth, text)}
        except EngineError as exc:  # a file that stopped rendering is a regression, graded 0
            row = {"file": name, "status": f"{type(exc).__name__}: {exc}", "pages": 0, "recall": 0.0}
        row.update(spike=spike, truth_words=sum(truth.values()), seconds=round(time.monotonic() - start, 2))
        rows.append(row)
    return rows


def summary(rows: list[dict]) -> dict:
    values = [r["recall"] for r in rows]
    return {
        "files": len(rows),
        "mean_recall": round(sum(values) / len(values), 4),
        "under_90": sorted(r["file"] for r in rows if r["recall"] < 0.9),
        "not_rendered": sorted(r["file"] for r in rows if r["status"] != "ok"),
        # per-file drops of more than 5 points against the spike: where to look first
        "dropped": {r["file"]: [round(r["spike"], 3), round(r["recall"], 3)] for r in rows
                    if r["recall"] < r["spike"] - 0.05},
    }


@pytest.mark.engine
def test_word_text_recall_floor(engine, fidelity_report):
    rows = grade(engine, BASELINE["word_recall"], client.TEXT)
    result = summary(rows)
    fidelity_report["word"] = {**result, "engine": engine.version, "rows": rows}
    print(f"\nWord: mean {result['mean_recall']:.1%} over {result['files']} files, "
          f"under 90%: {len(result['under_90'])}, dropped vs spike: {result['dropped']}")
    assert result["mean_recall"] >= WORD_MEAN_FLOOR, result
    assert len(result["under_90"]) <= WORD_UNDER_90_MAX, result


@pytest.mark.engine
def test_slides_text_recall_floor(engine, fidelity_report):
    rows = grade(engine, BASELINE["slides_recall"], client.SLIDES)
    result = summary(rows)
    fidelity_report["slides"] = {**result, "engine": engine.version, "rows": rows}
    print(f"\nSlides: mean {result['mean_recall']:.1%} over {result['files']} decks, "
          f"dropped vs spike: {result['dropped']}")
    assert result["mean_recall"] >= SLIDES_MEAN_FLOOR, result


@pytest.mark.engine
def test_spreadsheet_colours_floor(engine, fidelity_report):
    rows = []
    for name in BASELINE["coloured_workbooks"]:
        start = time.monotonic()
        try:
            book = workbook(corpus_path(name), engine=engine)
            grids = [book.grid(i) for i in range(len(book.sheets))]
            fills = sum(bool(cell.fill) for grid in grids for cell in grid.cells.values())
            status = "ok"
        except EngineError as exc:
            fills, status = 0, f"{type(exc).__name__}: {exc}"
        rows.append({"file": name, "status": status, "filled_cells": fills,
                     "seconds": round(time.monotonic() - start, 2)})
    shown = [r["file"] for r in rows if r["filled_cells"]]
    fidelity_report["colours"] = {"shown": len(shown), "of": len(rows), "engine": engine.version,
                                  "rows": rows}
    print(f"\nSpreadsheet colours shown: {len(shown)}/{len(rows)}")
    assert len(shown) >= COLOURED_FLOOR, [r for r in rows if not r["filled_cells"]]
