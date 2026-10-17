"""Fuzz the document engine path (Requirements 10.3–10.5; NFR Robustness).

Mutated Word, spreadsheet and slide files go through the same calls the views make: client.convert
(page 1, then the whole PDF) and sheets.workbook (every sheet's grid). Their modules document
EngineError and its subclasses: ConversionError (damaged, password) and EngineTimeout /
EngineCrashed, after which the engine restarts; those count as handled. Anything else, or a call
that outlives the watchdog, is a failure. After the campaign a pristine file must convert again:
a failure in one document must leave the engine usable for the next (10.5).
"""

import os

import pytest
from fuzzlib import FIXTURES, describe, iterations, run_campaign

from sovander.engine import client
from sovander.engine.process import EngineCrashed, EngineError, EngineTimeout
from sovander.engine.sheets import workbook
from tests.fidelity.engine_harness import running_engine

pytestmark = pytest.mark.engine

# Shorter than production's 60 s so a nightly finds more cases per hour; the kill-and-restart path is
# the same either way
JOB_TIMEOUT = float(os.environ.get("SOVANDER_FUZZ_JOB_TIMEOUT", "30"))
SEEDS = {
    client.TEXT: ["report.docx", "word-pages.docx", "cell-directions.docx", "letter.odt", "memo.rtf",
                  "legacy.doc"],
    client.SHEET: ["budget.xlsx", "budget.xls", "budget.ods", "budget.csv", "budget.xlsm"],
    client.SLIDES: ["deck.pptx", "lines.pptx", "symbol-bullets.pptx", "weights.pptx", "deck.pptm"],
}


@pytest.fixture(scope="module")
def engine():
    with running_engine("fuzz", job_timeout=JOB_TIMEOUT) as eng:
        yield eng


def open_with(engine, kind: str):
    if kind == client.SHEET:
        def target(data: bytes, name: str) -> str:
            book = workbook(data, engine=engine)
            for index in range(len(book.sheets)):
                book.grid(index)
            return "ok"
    else:
        def target(data: bytes, name: str) -> str:
            chunks = list(client.convert(data, kind, engine=engine))
            if not chunks[-1].final or not bytes(chunks[-1]).startswith(b"%PDF-"):
                raise AssertionError("the last chunk is not a whole PDF")
            return "damaged notice" if chunks[-1].damaged else "ok"
    return target


@pytest.mark.parametrize("kind", sorted(SEEDS))
def test_engine_survives_malformed_files(engine, kind):
    seeds = [FIXTURES / name for name in SEEDS[kind]]
    target = open_with(engine, kind)
    # The watchdog answers within JOB_TIMEOUT per job; a case runs up to three jobs plus a restart
    report = run_campaign("engine", kind, seeds, target, allowed=(EngineError,), timeout=4 * JOB_TIMEOUT + 60,
                          cases=iterations(engine=True))
    restarts = sum(report["outcomes"].get(e.__name__, 0) for e in (EngineTimeout, EngineCrashed))
    print(f"\nengine/{kind}: {report['cases']} cases in {report['seconds']} s, {report['outcomes']}, "
          f"restarts {restarts}")
    assert not report["failures"], describe(report)
    # Still serving documents after everything the campaign threw at it
    assert target(seeds[0].read_bytes(), seeds[0].name) in ("ok", "damaged notice")
