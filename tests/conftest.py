import gc
import os
import secrets
import socket
from pathlib import Path

import pytest

FIXTURES = Path(__file__).parent / "fixtures"
# Real-world files downloaded for the spike (Apache POI test data); engine tests skip without them
CORPUS = Path(__file__).parent.parent / "spike" / "corpus"
# The snap LibreOffice used in development cannot read /tmp or hidden folders
ENGINE_HOME = Path(__file__).resolve().parents[1] / "build" / "test-engine"


def _views_available() -> bool:
    """GTK 4 and libadwaita: absent where only the unit tests run (CI's unit job installs neither)."""
    try:
        import gi

        gi.require_version("Gtk", "4.0")
        gi.require_version("Adw", "1")
        return True
    except (ImportError, ValueError):
        return False


# Without GTK these cannot even be imported, and one import error fails the whole session
collect_ignore = [] if _views_available() else [
    "gui", "engine/test_open_pipeline.py", "engine/test_sheet_view_engine.py",
]


@pytest.fixture(scope="session", autouse=True)
def collect_on_main_thread():
    """As the app does: the cycle collector never runs in a worker thread, where it would finalise
    GTK objects off the main thread and GTK would abort; tests collect between themselves instead."""
    gc.disable()
    yield
    gc.enable()


@pytest.fixture(autouse=True)
def collect_between_tests():
    yield
    gc.collect()


@pytest.fixture(autouse=True)
def isolated_home(tmp_path, monkeypatch):
    """Every test gets its own data and cache folders, never the user's."""
    monkeypatch.setenv("SOVANDER_HOME", str(tmp_path / "home"))


@pytest.fixture
def fixture_file():
    def get(name: str) -> Path:
        path = FIXTURES / name
        assert path.exists(), f"missing fixture {name}"
        return path

    return get


# The document engine: one LibreOffice shared by every test that asks for `engine` (little RAM here)

_shared: dict = {}  # "engine": the shared Engine once started; "yielded": stopped for a suite of its own


def _test_address(soffice: str) -> str:
    """A UNO address of this session's own. Never the production pipe, nor SOVANDER_ENGINE_CONNECT,
    which development exports for the app: either would reach a Sovander the developer has open."""
    if soffice.startswith("/snap/"):
        # the snap keeps its pipe in a private /tmp the host cannot reach (development only)
        with socket.socket() as probe:
            probe.bind(("127.0.0.1", 0))
            return f"socket,host=127.0.0.1,port={probe.getsockname()[1]}"
    return f"pipe,name=sovander-test-{os.getpid()}-{secrets.token_hex(4)}"


def _engine_missing(reason: str) -> None:
    """No LibreOffice to test with: a skip, or a failure where SOVANDER_REQUIRE_ENGINE=1 forbids skipping."""
    if os.environ.get("SOVANDER_REQUIRE_ENGINE") == "1":
        pytest.fail(f"{reason} (SOVANDER_REQUIRE_ENGINE=1)", pytrace=False)
    pytest.skip(reason)


@pytest.fixture(scope="session")
def engine():
    """The session's LibreOffice, on an address of its own, stopped at the end.

    Skips when LibreOffice or its Python bridge is not installed. One that is installed and does
    not start fails every test that needs it: a skip there would hide a broken engine.
    """
    from sovander.engine.process import Engine, find_soffice

    soffice = find_soffice()
    if not soffice or not os.access(soffice, os.X_OK):
        _engine_missing("LibreOffice not found")
    try:
        import uno  # noqa: F401
    except ImportError:
        _engine_missing("pyuno not importable: run under scripts/dev-env.sh")
    patch = pytest.MonkeyPatch()  # session fixtures run before function ones: keep the engine log in build/
    patch.setenv("SOVANDER_HOME", str(ENGINE_HOME))
    ENGINE_HOME.mkdir(parents=True, exist_ok=True)
    eng = Engine(soffice=soffice, connect=_test_address(soffice), profile=ENGINE_HOME / "profile")
    try:
        eng.start()
    except Exception as exc:  # noqa: BLE001 - every failure to start is the same loud failure
        eng.stop()
        patch.undo()
        pytest.fail(f"LibreOffice is installed ({soffice}) but the engine did not start: {exc}",
                    pytrace=False)
    _shared.update(engine=eng, yielded=False)
    yield eng
    _shared.clear()
    eng.stop()
    patch.undo()


@pytest.hookimpl(tryfirst=True)
def pytest_runtest_setup(item):
    """One LibreOffice at a time. A module that starts engines of its own through
    tests/fidelity/engine_harness.py (fidelity, fuzz, perf) has the shared one stopped first;
    the next test that asks for `engine` starts it again."""
    eng = _shared.get("engine")
    if eng is None:
        return
    if hasattr(getattr(item, "module", None), "running_engine"):
        if eng.alive:
            eng.stop()
            _shared["yielded"] = True
    elif _shared["yielded"] and "engine" in item.fixturenames:
        _shared["yielded"] = False
        eng.start()
