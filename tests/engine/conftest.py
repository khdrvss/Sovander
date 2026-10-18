"""Engine tests take the session's one LibreOffice, the `engine` fixture in tests/conftest.py.

Run them with ``scripts/dev-env.sh python3 -m pytest -m engine tests/engine``; without pyuno or
LibreOffice they skip, and with a LibreOffice that does not start they fail.
"""

import re
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
# The snap LibreOffice used in development cannot read /tmp or hidden folders
ENGINE_HOME = ROOT / "build" / "test-engine"


@pytest.fixture(autouse=True)
def engine_home(monkeypatch):
    ENGINE_HOME.mkdir(parents=True, exist_ok=True)
    monkeypatch.setenv("SOVANDER_HOME", str(ENGINE_HOME))
    return ENGINE_HOME


@pytest.fixture
def corpus_file():
    corpus = ROOT / "spike" / "corpus"

    def get(name: str) -> Path:
        path = corpus / name
        if not path.exists():
            pytest.skip(f"spike corpus file {name} not present")
        return path

    return get


@pytest.fixture
def repairs_ooxml(engine):
    """Skips before LibreOffice 24.2, which opens .docx/.xlsx/.pptx zips without repair even when the repair
    is approved (tdf#96401, commit 86c682273d90): 7.3 on Ubuntu 22.04 reports a cut .docx as damaged."""
    found = re.search(r"(\d+)\.(\d+)", engine.version)
    if found and (int(found[1]), int(found[2])) < (24, 2):
        pytest.skip(f"{engine.version} cannot repair a damaged OOXML zip (tdf#96401, fixed in 24.2)")
