"""The shared `engine` fixture in tests/conftest.py (task C5): an address of its own, and a loud
failure for a LibreOffice that is installed but does not start."""

import os
import subprocess
import sys
from pathlib import Path

import pytest

from sovander.engine.process import connection_string

pytestmark = pytest.mark.engine

ROOT = Path(__file__).resolve().parents[2]


def test_the_session_engine_has_an_address_of_its_own(engine, monkeypatch):
    monkeypatch.delenv("SOVANDER_ENGINE_CONNECT", raising=False)
    production = connection_string()
    assert engine.connect != production
    assert engine.connect.startswith(("pipe,name=sovander-test-", "socket,host=127.0.0.1,port="))


def run_one_engine_test(tmp_path, soffice: str) -> subprocess.CompletedProcess:
    """A separate pytest session whose engine is ``soffice``; it never starts a real LibreOffice."""
    env = dict(os.environ, SOVANDER_SOFFICE=soffice)
    env.pop("SOVANDER_REQUIRE_ENGINE", None)
    return subprocess.run(
        [sys.executable, "-m", "pytest", "-q", "-p", "no:cacheprovider", "-m", "engine",
         "tests/engine/test_process.py::test_engine_runs_and_names_its_version"],
        cwd=ROOT, env=env, capture_output=True, text=True, timeout=120,
    )


def test_an_installed_engine_that_does_not_start_fails_loudly(tmp_path):
    pytest.importorskip("uno", reason="pyuno not importable: run under scripts/dev-env.sh")
    broken = tmp_path / "soffice"
    broken.write_text("#!/bin/sh\nexit 3\n")
    broken.chmod(0o755)
    run = run_one_engine_test(tmp_path, str(broken))
    assert run.returncode == 1, run.stdout + run.stderr
    assert "is installed" in run.stdout and "but the engine did not start" in run.stdout, run.stdout
    assert "1 error" in run.stdout, run.stdout


def test_no_engine_installed_skips(tmp_path):
    run = run_one_engine_test(tmp_path, str(tmp_path / "no-soffice-here"))
    assert run.returncode == 0, run.stdout + run.stderr
    assert "1 skipped" in run.stdout, run.stdout
