"""The application (Requirements 1.1-1.3, 1.6, 9.7, 11.1, 12.1, 12.4).

One window per document, a home window when started without files, files from the command line,
the file manager, drops and the file chooser. At start it applies the language and theme, runs the
offline self-check, and starts the document engine on a worker thread, so PDFs and images open at
once while the engine warms up.
"""

from __future__ import annotations

import logging
import os
import signal
import sys
import threading
from pathlib import Path

import gi

gi.require_version("Adw", "1")
gi.require_version("Gtk", "4.0")
from gi.repository import Adw, Gio, GLib  # noqa: E402

from sovander import APP_ID, i18n, offline  # noqa: E402
from sovander.document_context import Context  # noqa: E402
from sovander.engine import process  # noqa: E402
from sovander.engine.process import EngineError  # noqa: E402
from sovander.open_jobs import Source  # noqa: E402
from sovander.store import Store  # noqa: E402
from sovander.views import collect_garbage_on_main_thread  # noqa: E402

log = logging.getLogger(__name__)

ACCELS = {
    "win.open": ["<Control>o"],
    "win.print": ["<Control>p"],
    "win.goto": ["<Control>l"],
    "win.zoom-in": ["<Control>plus", "<Control>equal", "<Control>KP_Add"],
    "win.zoom-out": ["<Control>minus", "<Control>KP_Subtract"],
    "win.night": ["<Control>i"],
    "win.present": ["F5"],
    "win.fullscreen": ["F11"],
    "win.close": ["<Control>w"],
    "app.quit": ["<Control>q"],
}  # Ctrl+F belongs to the find bar (find.FindBar.attach); F10 opens the main menu
THEMES = {"light": Adw.ColorScheme.FORCE_LIGHT, "dark": Adw.ColorScheme.FORCE_DARK}  # "system": the default
ENGINE_STOP_WAIT = 10.0


class SovanderApp(Adw.Application):
    """``engine`` and ``cache_root`` replace the shared engine and the render cache folder (tests)."""

    def __init__(self, application_id: str = APP_ID, store: Store | None = None, engine=None,
                 warm_engine: bool = True, unique: bool = True, cache_root: Path | None = None):
        flags = Gio.ApplicationFlags.HANDLES_OPEN
        if not unique:
            flags |= Gio.ApplicationFlags.NON_UNIQUE
        super().__init__(application_id=application_id, flags=flags)
        self.store = store
        self.context: Context | None = None
        self.offline_status: offline.OfflineStatus | None = None
        self._engine, self._warm, self._cache_root = engine, warm_engine, cache_root

    def do_startup(self) -> None:
        Adw.Application.do_startup(self)
        if self.store is None:
            self.store = Store()
        settings = self.store.settings
        try:
            i18n.set_language(settings.language)
        except ValueError:
            i18n.set_language(None)
        if settings.theme in THEMES:  # otherwise libadwaita follows the system (12.1)
            Adw.StyleManager.get_default().set_color_scheme(THEMES[settings.theme])
        self.offline_status = offline_status()
        self.context = Context(self.store, self._engine, self._cache_root)
        self.store.add_forget_hook(self.context.forget)
        quit_action = Gio.SimpleAction.new("quit", None)
        quit_action.connect("activate", lambda *_args: self.quit())
        self.add_action(quit_action)
        for action, accels in ACCELS.items():
            self.set_accels_for_action(action, accels)
        if self._warm:
            threading.Thread(target=warm_up, args=(self.context,), name="sovander-engine-warm-up",
                             daemon=True).start()

    def do_activate(self) -> None:
        """Started without files: the home screen (11.1), or the home window already open."""
        from sovander.window import SovanderWindow

        for window in self.get_windows():
            if isinstance(window, SovanderWindow) and window.page is None:
                window.present()
                return
        SovanderWindow(self).present()

    def do_open(self, files, _n_files, _hint) -> None:
        """Files from the command line or the file manager (1.1, 1.3)."""
        sources = []
        for file in files:
            path = file.get_path()
            sources.append(Source.from_path(path) if path else Source(file.get_basename() or file.get_uri()))
        self.open_sources(sources)

    def open_paths(self, paths, window=None) -> None:
        self.open_sources([Source.from_path(path) for path in paths], window)

    def open_sources(self, sources, window=None) -> list:
        """Each document in its own window (1.3); ``window`` takes the first one if it is empty.

        A file that is already open brings its window forward instead of opening twice.
        """
        from sovander.window import SovanderWindow

        shown = []
        for source in sources:
            target = self._window_showing(source)
            if target is None and window is not None and window.page is None:
                target, window = window, None
                target.load(source)
            elif target is None:
                target = SovanderWindow(self, source)
            target.present()
            shown.append(target)
        return shown

    def _window_showing(self, source: Source):
        if source.path is None:
            return None
        wanted = _resolved(source.path)
        for window in self.get_windows():
            page = getattr(window, "page", None)
            if page is not None and page.source.path is not None and _resolved(page.source.path) == wanted:
                return window
        return None

    def set_night(self, on: bool) -> None:
        """Night mode in every window (12.2)."""
        for window in self.get_windows():
            if hasattr(window, "apply_night"):
                window.apply_night(on)

    def do_shutdown(self) -> None:
        for window in self.get_windows():
            page = getattr(window, "page", None)
            if page is not None:
                page.save_position()
        if self._engine is None and self._warm:
            stop_engine(process.shared())
        Adw.Application.do_shutdown(self)


def offline_status() -> offline.OfflineStatus:
    """The self-check (9.7). The launcher sets SOVANDER_CONFINEMENT=none when it could not enter the
    AppArmor profile; then a check that could not decide counts as unprotected."""
    status = offline.check()
    if status is offline.OfflineStatus.UNKNOWN and os.environ.get("SOVANDER_CONFINEMENT") == "none":
        status = offline.OfflineStatus.UNPROTECTED
    return status


def warm_up(context: Context) -> None:
    """Start the engine before the first office file asks for it (worker thread)."""
    engine = context.engine
    if not engine.soffice:
        log.info("no document engine installed; office files will say how to install it")
        return
    try:
        engine.start()
        context.cache(engine)  # hashes the font folders and remembers the engine version
    except EngineError as exc:
        log.warning("the document engine did not start: %s", exc)


def stop_engine(engine, wait: float = ENGINE_STOP_WAIT) -> None:
    """Stop LibreOffice; if a job or the start-up holds it past ``wait`` seconds, kill it."""
    worker = threading.Thread(target=engine.stop, name="sovander-engine-stop", daemon=True)
    worker.start()
    worker.join(wait)
    if worker.is_alive() and engine.pid:
        try:
            os.killpg(engine.pid, signal.SIGKILL)  # the engine runs in its own session (process.py)
        except OSError:
            pass
        worker.join(5)


def _resolved(path: Path) -> Path:
    try:
        return path.resolve()
    except OSError:
        return path.absolute()


def run(argv: list[str] | None = None) -> int:
    GLib.set_prgname("sovander")  # before WebKit starts: it names its cache folders after the program
    GLib.set_application_name("Sovander")
    level = os.environ.get("SOVANDER_LOG", "WARNING").upper()
    logging.basicConfig(level=getattr(logging, level, logging.WARNING),
                        format="%(asctime)s %(name)s %(levelname)s: %(message)s")
    app = SovanderApp()
    collect_garbage_on_main_thread()  # GTK objects must never be finalised by a worker thread's collection
    # Quit through shutdown on Ctrl+C, kill or logout: the engine runs in a session of its own and
    # would outlive an app that just died. GLib's handler also works where a shell ignores SIGINT.
    for signum in (signal.SIGINT, signal.SIGTERM, signal.SIGHUP):
        GLib.unix_signal_add(GLib.PRIORITY_DEFAULT, signum, _quit_on_signal, app)
    return app.run(sys.argv if argv is None else argv)


def _quit_on_signal(app: SovanderApp) -> bool:
    app.quit()
    return GLib.SOURCE_CONTINUE
