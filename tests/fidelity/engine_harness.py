"""A document engine for one test module of the fidelity, fuzz and perf suites.

tests/engine/conftest.py keeps one engine for the whole session. These suites take one per module
instead and stop it when the module ends, so at most one of theirs runs at a time (the development
machine is short of RAM). Each suite has its own profile folder and its own UNO pipe name: two
engines on one name would answer for each other, and the production name would reach a Sovander the
developer has open.

CI sets SOVANDER_REQUIRE_ENGINE=1: there a missing engine fails instead of skipping, because a
suite that skips everything passes silently.
"""

from __future__ import annotations

import os
import socket
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path

import pytest

from sovander.engine.process import Engine, find_soffice

ROOT = Path(__file__).resolve().parents[2]


def required(variable: str) -> bool:
    return os.environ.get(variable) == "1"


def unavailable(reason: str, variable: str) -> None:
    """Skip the test, or fail it when `variable`=1 says this run must not skip."""
    if required(variable):
        pytest.fail(f"{reason} ({variable}=1)", pytrace=False)
    pytest.skip(reason)


def _free_port() -> int:
    with socket.socket() as probe:
        probe.bind(("127.0.0.1", 0))
        return probe.getsockname()[1]


def connect_string(soffice: str, suite: str) -> str:
    configured = os.environ.get("SOVANDER_ENGINE_CONNECT")
    if configured:
        return configured
    if soffice.startswith("/snap/"):
        # the snap keeps its pipe in a private /tmp the host cannot reach (development only)
        return f"socket,host=127.0.0.1,port={_free_port()}"
    return f"pipe,name=sovander-test-{suite}-{os.getpid()}"


def engine_home(suite: str) -> Path:
    # Inside the checkout: the snap LibreOffice used in development cannot read /tmp or dot folders
    home = ROOT / "build" / f"test-engine-{suite}"
    home.mkdir(parents=True, exist_ok=True)
    return home


@contextmanager
def running_engine(suite: str, job_timeout: float | None = None, start: bool = True) -> Iterator[Engine]:
    """An Engine with its own profile and pipe, stopped on exit. `start=False` leaves starting to the
    caller (the cold-start measurement)."""
    soffice = find_soffice()
    if not soffice or not os.access(soffice, os.X_OK):
        unavailable("LibreOffice not found", "SOVANDER_REQUIRE_ENGINE")
    try:
        import uno  # noqa: F401
    except ImportError:
        unavailable("pyuno not importable: run under scripts/dev-env.sh", "SOVANDER_REQUIRE_ENGINE")
    home = engine_home(suite)
    patch = pytest.MonkeyPatch()
    patch.setenv("SOVANDER_HOME", str(home))  # the engine log goes to build/, next to the profile
    options = {} if job_timeout is None else {"job_timeout": job_timeout}
    engine = Engine(soffice=soffice, connect=connect_string(soffice, suite), profile=home / "profile",
                    **options)
    try:
        if start:
            try:
                engine.start()
            except Exception as exc:  # noqa: BLE001 - reported as the reason for the skip or failure
                unavailable(f"engine did not start: {exc}", "SOVANDER_REQUIRE_ENGINE")
        yield engine
    finally:
        engine.stop()
        patch.undo()
