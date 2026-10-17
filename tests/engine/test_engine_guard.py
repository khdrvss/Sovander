"""The engine never outlives the app (found end to end: GTK exits on a lost display, atexit never runs)."""

import os
import signal
import subprocess
import sys
import time
from pathlib import Path

import pytest

from sovander.engine.process import find_soffice
from tests.fidelity.engine_harness import connect_string, engine_home, running_engine, unavailable

pytestmark = pytest.mark.engine
REPO = Path(__file__).resolve().parents[2]
APP = """\
import os, sys
from pathlib import Path
from sovander.engine.process import Engine
engine = Engine(connect=sys.argv[1], profile=Path(sys.argv[2]))
engine.start()
print(engine.pid, flush=True)
os._exit(1)  # no atexit, no stop: as when GTK exits on a lost display, or a crash
"""


def group_alive(group: int) -> bool:
    try:
        os.killpg(group, 0)
        return True
    except ProcessLookupError:
        return False


def gone(group: int, timeout: float = 10.0) -> bool:
    end = time.monotonic() + timeout
    while group_alive(group):
        if time.monotonic() > end:
            return False
        time.sleep(0.1)
    return True


def test_a_stopped_engine_takes_its_guard_and_leaves_nothing():
    with running_engine("guard") as engine:
        group, guard = engine.pid, engine._guard[0]
        assert guard.poll() is None and group_alive(group)
    assert gone(group)
    assert guard.poll() == 0  # it left after "keep" without killing anything


def test_the_engine_dies_with_an_app_that_ends_without_cleaning_up():
    soffice = find_soffice()
    if not soffice or not os.access(soffice, os.X_OK):
        unavailable("LibreOffice not found", "SOVANDER_REQUIRE_ENGINE")
    home = engine_home("guard-app")
    argv = [sys.executable, "-c", APP, connect_string(soffice, "guard-app"), str(home / "profile")]
    app = subprocess.run(argv, capture_output=True, text=True, timeout=180, cwd=REPO,
                         env={**os.environ, "SOVANDER_HOME": str(home)})
    assert app.returncode == 1 and app.stdout.strip().isdigit(), app.stderr[-2000:]
    group = int(app.stdout)
    try:
        assert gone(group), "the engine outlived its app"
    finally:
        if group_alive(group):  # the engine this test's app started: never leave it running
            os.killpg(group, signal.SIGKILL)
