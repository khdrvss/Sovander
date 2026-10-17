"""Fidelity suite (NFR Fidelity, task 6.3): the spike's corpus through the real engine.

The corpus comes from scripts/fetch-corpus.sh, never from a test. Without it everything here skips;
with SOVANDER_REQUIRE_CORPUS=1 (the nightly job) a missing corpus fails instead.
Results go to build/fidelity/report.json, one section per test.
"""

import json

import pytest
from corpus_lock import ROOT

REPORT = ROOT / "build" / "fidelity" / "report.json"


@pytest.fixture(scope="session")
def fidelity_report():
    sections: dict = {}
    yield sections
    if sections:
        REPORT.parent.mkdir(parents=True, exist_ok=True)
        REPORT.write_text(json.dumps(sections, indent=1, ensure_ascii=False) + "\n", encoding="utf-8")
