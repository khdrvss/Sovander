"""The document engine: one headless LibreOffice kept warm, reached over UNO (Requirements 9.1, 10.5).

Production talks to it over an AF_UNIX pipe, so an AppArmor profile that denies every inet socket breaks
nothing. The snap LibreOffice used in development keeps its pipe in a private /tmp the host cannot reach,
so development sets SOVANDER_ENGINE_CONNECT (e.g. ``socket,host=127.0.0.1,port=2083``) instead.
"""

from __future__ import annotations

import atexit
import logging
import os
import re
import shutil
import signal
import subprocess
import tempfile
import threading
import time
import traceback
import weakref
from collections.abc import Callable
from pathlib import Path
from typing import Any, TypeVar

from .. import paths

log = logging.getLogger(__name__)
T = TypeVar("T")

DEFAULT_SOFFICE = "/usr/lib/libreoffice/program/soffice"
PROFILE_SEED = Path(__file__).resolve().parents[2] / "data" / "engine-profile.xcu"
JOB_TIMEOUT = 60.0
START_TIMEOUT = 120.0
# soffice listens ~0.7 s after spawn (.deb 24.2); a 0.2 s poll added ~0.1 s on average to every cold start
CONNECT_POLL = 0.02
LOG_LIMIT = 1 << 20
# The engine runs in a session of its own, so it outlives an app that ends without its atexit: a
# crash, SIGKILL, or GTK exiting when the display goes away. A guard per engine reads a pipe that
# only this process writes; however this process ends, the kernel closes the pipe and the guard
# kills the engine's process group ($0). A normal stop writes "keep" first.
_GUARD = 'read word; [ "$word" = keep ] || kill -9 "-$0" 2>/dev/null'  # dash: only -9, no --
_ITEM_KEY = re.compile(r'<item oor:path="([^"]*)"><prop oor:name="([^"]*)"')


class EngineError(Exception):
    """Base for every engine failure; the message is safe to show to the user."""


class EngineUnavailable(EngineError):
    """LibreOffice is not installed, would not start, or cannot be reached."""


class EngineTimeout(EngineError):
    """A job ran past its time limit; the engine was killed and is being restarted."""


class EngineCrashed(EngineError):
    """The engine died during a job; it is being restarted."""


def find_soffice() -> str | None:
    """SOVANDER_SOFFICE, then soffice on PATH, then Ubuntu's install path."""
    configured = os.environ.get("SOVANDER_SOFFICE")
    if configured:
        return configured
    found = shutil.which("soffice")
    if found:
        return found
    return DEFAULT_SOFFICE if os.access(DEFAULT_SOFFICE, os.X_OK) else None


def connection_string() -> str:
    """UNO connection description shared by --accept and the resolver."""
    return os.environ.get("SOVANDER_ENGINE_CONNECT") or f"pipe,name=sovander-{os.getuid()}"


def seed_profile(profile: Path, seed: Path = PROFILE_SEED) -> None:
    """Merge our settings into the profile's user layer, keeping whatever else LibreOffice stored there."""
    target = profile / "user" / "registrymodifications.xcu"
    seed_text = seed.read_text(encoding="utf-8")
    ours = [line for line in seed_text.splitlines() if _key(line)]
    keys = {_key(line) for line in ours}
    if target.exists():
        lines = target.read_text(encoding="utf-8").splitlines()
        if "</oor:items>" not in lines:
            lines = seed_text.splitlines()  # unreadable user layer: start again from ours
    else:
        lines = seed_text.splitlines()
    kept = [line for line in lines if _key(line) not in keys]
    end = kept.index("</oor:items>")
    _write_atomic(target, "\n".join(kept[:end] + ours + kept[end:]) + "\n")


def _key(line: str) -> tuple[str, str] | None:
    """(path, property) of one registrymodifications item line; LibreOffice writes one item per line."""
    match = _ITEM_KEY.search(line)
    return match.groups() if match else None


def _write_atomic(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=path.parent, prefix=".tmp-")
    with os.fdopen(fd, "w", encoding="utf-8") as fh:
        fh.write(text)
    os.replace(tmp, path)


def _patch_pyuno(uno: Any) -> None:
    """Let pyuno hand UNO exceptions (password and repair requests) to Python 3.12.

    pyuno sets ``args`` on the new exception and its ``__setattr__`` forwards that to the UNO struct,
    which has no such field, so ``request.getRequest()`` fails. Classes are built after this runs,
    so they pick up the fixed setter; on a pyuno without the bug nothing changes.
    """
    original = uno._uno_struct__setattr__
    if getattr(original, "sovander_patched", False):
        return

    def setattr_(self: Any, name: str, value: Any) -> None:
        if name == "args" and isinstance(self, BaseException):
            BaseException.__setattr__(self, name, value)
        else:
            original(self, name, value)

    setattr_.sovander_patched = True
    uno._uno_struct__setattr__ = setattr_


def _start_guard(group: int) -> tuple[subprocess.Popen, int] | None:
    """The guard for the engine's process group (see _GUARD), or None if sh cannot start."""
    read_end, write_end = os.pipe()  # not inheritable (PEP 446): no other child holds the pipe open
    try:
        guard = subprocess.Popen(["sh", "-c", _GUARD, str(group)], stdin=read_end, stdout=subprocess.DEVNULL,
                                 stderr=subprocess.DEVNULL, start_new_session=True)  # Ctrl+C spares it
    except OSError as exc:
        os.close(write_end)
        log.warning("no guard for the engine: %s", exc)
        return None
    finally:
        os.close(read_end)
    return guard, write_end


def _release_guard(guard: subprocess.Popen, write_end: int) -> None:
    """The engine was stopped on purpose: the guard leaves without killing."""
    try:
        os.write(write_end, b"keep\n")
    except OSError:
        pass
    finally:
        os.close(write_end)
    try:
        guard.wait(2)
    except subprocess.TimeoutExpired:
        guard.kill()
        guard.wait()


def is_bridge_failure(exc: BaseException) -> bool:
    name = type(exc).__name__
    return name == "DisposedException" or "bridge" in str(exc).lower() or "disposed" in str(exc).lower()


class Engine:
    """Starts LibreOffice once, runs jobs one at a time under a watchdog, restarts it after a hang or crash.

    A job is ``job(engine) -> result`` and uses ``engine.ctx``, ``engine.desktop`` and
    ``engine.create(...)``. UNO references die with a restart: jobs must not keep them.
    """

    def __init__(
        self,
        soffice: str | None = None,
        connect: str | None = None,
        profile: Path | None = None,
        job_timeout: float = JOB_TIMEOUT,
        start_timeout: float = START_TIMEOUT,
        seed: Path = PROFILE_SEED,
    ):
        self.soffice = soffice or find_soffice()
        self.connect = connect or connection_string()
        self.profile = profile or paths.cache_dir() / "engine"
        self.job_timeout = job_timeout
        self.start_timeout = start_timeout
        self.seed = seed
        self.generation = 0  # bumped on every start, so callers can tell their UNO objects are stale
        self.ctx: Any = None
        self.desktop: Any = None
        self._proc: subprocess.Popen | None = None
        self._guard: tuple[subprocess.Popen, int] | None = None  # the guard and our end of its pipe
        self._lock = threading.RLock()
        self._stopped = False
        self._version: str | None = None
        self._atexit = False

    @property
    def alive(self) -> bool:
        return self._proc is not None and self._proc.poll() is None and self.desktop is not None

    @property
    def pid(self) -> int | None:
        return self._proc.pid if self._proc else None

    @property
    def version(self) -> str:
        """Product name and version, e.g. "LibreOffice 26.2.5.2"; part of every render cache key."""
        if self._version is None:
            self.run(lambda _engine: None)  # starting the engine reads the version
        return self._version or "unknown"

    def start(self) -> None:
        """Start the engine and wait until it answers over UNO; no-op when it already runs."""
        with self._lock:
            self._stopped = False
            if self.alive:
                return
            self._cleanup()
            self._launch()

    def stop(self) -> None:
        """Ask LibreOffice to quit, then make sure nothing we started is left running."""
        self._stopped = True
        with self._lock:
            if self.desktop is not None and self._proc is not None and self._proc.poll() is None:
                try:
                    self.desktop.terminate()
                except Exception:  # noqa: BLE001 - the bridge drops as soffice exits; that is success
                    pass
                try:
                    self._proc.wait(15)
                except subprocess.TimeoutExpired:
                    pass
            self._cleanup()

    def run(self, job: Callable[[Engine], T], timeout: float | None = None) -> T:
        """Run one job with a time limit. Raises EngineTimeout or EngineCrashed and restarts the engine."""
        with self._lock:
            self.start()
            box: dict[str, Any] = {}

            def work() -> None:
                try:
                    box["result"] = job(self)
                except BaseException as exc:  # noqa: BLE001 - handed back to the caller's thread
                    box["error"] = exc

            worker = threading.Thread(target=work, name="sovander-engine-job", daemon=True)
            worker.start()
            worker.join(self.job_timeout if timeout is None else timeout)
            if worker.is_alive():
                log.warning("engine job timed out; restarting the engine")
                self._cleanup()  # killing soffice disposes the bridge, which unblocks the worker
                worker.join(5)
                self._restart_later()
                raise EngineTimeout("The document took too long to prepare and was stopped.")
            if "error" in box:
                exc = box["error"]
                # The job's frames hold its UNO proxies. Once the engine is gone, repr() of one aborts
                # the whole process (pyuno lets the bridge's DisposedException escape), and a traceback
                # shown with its arguments does that: the job is over, so its locals go
                traceback.clear_frames(exc.__traceback__)
                if not self._bridge_ok(exc):
                    log.warning("engine died during a job: %s", exc)
                    self._cleanup()
                    self._restart_later()
                    raise EngineCrashed("The document engine stopped unexpectedly.") from exc
                raise exc
            return box["result"]

    def create(self, service: str, *args: Any) -> Any:
        """Instantiate a UNO service inside the engine."""
        smgr = self.ctx.ServiceManager
        if args:
            return smgr.createInstanceWithArgumentsAndContext(service, args, self.ctx)
        return smgr.createInstanceWithContext(service, self.ctx)

    # ------------------------------------------------------------------ internals

    def _launch(self) -> None:
        if not self.soffice or not (os.access(self.soffice, os.X_OK) or shutil.which(self.soffice)):
            raise EngineUnavailable("The document engine (LibreOffice) is not installed.")
        if self.soffice.startswith("/snap/") and self.connect.startswith("pipe"):
            raise EngineUnavailable(
                "The snap LibreOffice keeps its pipe in a private /tmp; for development set "
                "SOVANDER_ENGINE_CONNECT=socket,host=127.0.0.1,port=<n>."
            )
        try:
            import uno
        except ImportError as exc:
            raise EngineUnavailable("The LibreOffice Python bridge (python3-uno) is not installed.") from exc
        _patch_pyuno(uno)
        self.profile.mkdir(parents=True, exist_ok=True)
        seed_profile(self.profile, self.seed)
        logfile = paths.cache_dir() / "engine.log"
        if logfile.exists() and logfile.stat().st_size > LOG_LIMIT:
            logfile.unlink()
        cmd = [
            self.soffice,
            "-env:UserInstallation=" + self.profile.resolve().as_uri(),
            "--headless", "--invisible", "--norestore", "--nologo", "--nodefault", "--nolockcheck",
            f"--accept={self.connect};urp;StarOffice.ComponentContext",
        ]
        with open(logfile, "ab") as out:
            # own session: killpg reaches the launcher and soffice.bin, and nothing we did not start
            self._proc = subprocess.Popen(cmd, stdin=subprocess.DEVNULL, stdout=out, stderr=out,
                                          start_new_session=True)
        self._guard = _start_guard(self._proc.pid)
        try:
            self._connect()
        except BaseException:
            self._cleanup()
            raise
        self.generation += 1
        if not self._atexit:
            ref = weakref.ref(self)
            atexit.register(lambda: ref() and ref().stop())
            self._atexit = True

    def _connect(self) -> None:
        import uno

        local = uno.getComponentContext()
        resolver = local.ServiceManager.createInstanceWithContext("com.sun.star.bridge.UnoUrlResolver", local)
        url = f"uno:{self.connect};urp;StarOffice.ComponentContext"
        deadline = time.monotonic() + self.start_timeout
        while True:
            try:
                self.ctx = resolver.resolve(url)
                break
            except Exception as exc:  # noqa: BLE001 - NoConnectException until soffice listens
                if self._proc.poll() is not None:
                    raise EngineUnavailable(f"The document engine exited during start-up "
                                            f"(code {self._proc.returncode}).") from exc
                if time.monotonic() > deadline:
                    raise EngineUnavailable("The document engine did not start in time.") from exc
                time.sleep(CONNECT_POLL)
        self.desktop = self.create("com.sun.star.frame.Desktop")
        self._version = self._read_version()

    def _read_version(self) -> str:
        from com.sun.star.beans import PropertyValue

        arg = PropertyValue()
        arg.Name, arg.Value = "nodepath", "/org.openoffice.Setup/Product"
        provider = self.create("com.sun.star.configuration.ConfigurationProvider")
        node = provider.createInstanceWithArguments("com.sun.star.configuration.ConfigurationAccess", (arg,))
        return f"{node.getByName('ooName')} {node.getByName('ooSetupVersionAboutBox')}"

    def _bridge_ok(self, exc: BaseException) -> bool:
        """False when the job's failure means the engine itself is gone."""
        if self._proc is None or self._proc.poll() is not None or self.desktop is None:
            return False
        if not is_bridge_failure(exc):
            return True
        try:  # a closed document also raises DisposedException; a live engine still answers
            self.desktop.getCurrentComponent()
            return True
        except Exception:  # noqa: BLE001
            return False

    def _cleanup(self) -> None:
        """Forget the bridge and kill our process group; safe to call when nothing runs."""
        self.ctx = self.desktop = None
        proc, self._proc = self._proc, None
        if proc is None:
            return
        try:
            os.killpg(proc.pid, signal.SIGKILL)
        except (ProcessLookupError, PermissionError):
            pass
        try:
            proc.wait(10)
        except subprocess.TimeoutExpired:
            log.error("engine process %s did not exit after SIGKILL", proc.pid)
        guard, self._guard = self._guard, None
        if guard is not None:
            _release_guard(*guard)

    def _restart_later(self) -> None:
        def restart() -> None:
            with self._lock:
                if self._stopped or self.alive:
                    return
                try:
                    self._launch()
                except EngineError as exc:
                    log.error("engine restart failed: %s", exc)  # the next job tries again

        threading.Thread(target=restart, name="sovander-engine-restart", daemon=True).start()


_shared: Engine | None = None
_shared_lock = threading.Lock()


def shared() -> Engine:
    """The app-wide engine, created on first use (started lazily by the first job)."""
    global _shared
    with _shared_lock:
        if _shared is None:
            _shared = Engine()
        return _shared
