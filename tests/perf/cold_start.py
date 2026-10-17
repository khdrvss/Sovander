"""Cold start (NFR Performance): app launch to page 1 drawn, with the document engine not running.

Every launch is a fresh Python process running the real app (SovanderApp, the engine warm-up, the open
pipeline, the page view); it ends when page 1 is drawn. Medians of N runs go to
build/perf/cold-start.json.

    scripts/dev-env.sh xvfb-run -a python3 -m tests.perf.cold_start [--runs 5] [--evict] [FILE ...]

Scenarios per file: "first" launches with no engine profile (the first launch after installing),
"later" with the profile an earlier launch left (every launch after that). The engine alone (spawn to
UNO-ready, Engine.start) is timed the same two ways. --evict drops LibreOffice's files and every
LD_LIBRARY_PATH folder from the page cache before each run (posix_fadvise, no root needed), which is
what the first launch after a reboot reads from disk.
"""

from __future__ import annotations

import argparse
import json
import os
import shutil
import statistics
import subprocess
import sys
import tempfile
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
REPORT = ROOT / "build" / "perf" / "cold-start.json"
DEFAULT_FILES = (ROOT / "tests" / "fixtures" / "word-pages.docx",
                 ROOT / "spike" / "corpus" / "docx" / "drawing.docx")
MARKS = ("imported", "engine_launch", "engine_ready", "page1_pdf", "first_page")
TIMEOUT = 120.0
PROFILE = Path("cache", "sovander", "engine")  # paths.cache_dir() / "engine" under SOVANDER_HOME


# ------------------------------------------------------------------ child: one real launch


def child(path: str) -> None:
    """Run the app on one file; print the monotonic time of each step once page 1 is drawn."""
    marks = {"python_up": time.monotonic()}
    import gi

    gi.require_version("Adw", "1")
    gi.require_version("Gtk", "4.0")
    from gi.repository import GLib

    GLib.set_prgname("sovander")
    from sovander import app as app_module
    from sovander.engine import client, process
    from sovander.views.page_view import PageView

    marks["imported"] = time.monotonic()
    launch, convert = process.Engine._launch, client.convert

    def timed_launch(engine):
        marks.setdefault("engine_launch", time.monotonic())
        launch(engine)
        marks.setdefault("engine_ready", time.monotonic())

    def timed_convert(*args, **kwargs):
        chunks = convert(*args, **kwargs)
        try:
            for chunk in chunks:
                marks.setdefault("page1_pdf", time.monotonic())
                yield chunk
        finally:
            chunks.close()

    process.Engine._launch, client.convert = timed_launch, timed_convert
    app = app_module.SovanderApp(application_id=f"io.github.khdrvss.Perf{os.getpid()}", unique=False)
    deadline = time.monotonic() + TIMEOUT

    def poll() -> bool:
        for window in app.get_windows():
            view = getattr(getattr(window, "page", None), "view", None)
            if isinstance(view, PageView) and view.page_count >= 1 and view.is_page_rendered(0):
                marks["first_page"] = time.monotonic()
                app.quit()
                return GLib.SOURCE_REMOVE
        if time.monotonic() > deadline:
            app.quit()
            return GLib.SOURCE_REMOVE
        return GLib.SOURCE_CONTINUE

    GLib.timeout_add(5, poll)
    app.run([sys.argv[0], path])  # quitting stops the engine (SovanderApp.do_shutdown)
    print("PROBE " + json.dumps(marks), flush=True)


# ------------------------------------------------------------------ parent


def evict(folders: list[Path]) -> int:
    """Drop these folders' files from the page cache; returns how many files were advised."""
    count = 0
    for folder in folders:
        for dirpath, _dirs, files in os.walk(folder, followlinks=True):
            for name in files:
                try:
                    fd = os.open(os.path.join(dirpath, name), os.O_RDONLY)
                except OSError:
                    continue
                try:
                    os.posix_fadvise(fd, 0, 0, os.POSIX_FADV_DONTNEED)
                    count += 1
                finally:
                    os.close(fd)
    return count


def evict_folders(soffice: str) -> list[Path]:
    """LibreOffice's install tree (program/..), its registry, and the LD_LIBRARY_PATH folders."""
    install = Path(soffice).resolve().parents[1]
    folders = [install, (install / "share" / "registry").resolve()]
    folders += [Path(p) for p in os.environ.get("LD_LIBRARY_PATH", "").split(":") if p]
    return [f for f in dict.fromkeys(folders) if f.is_dir() and f != Path("/")]


def launch(path: Path, home: Path, connect: str, folders: list[Path]) -> dict:
    if folders:
        evict(folders)
    env = dict(os.environ, SOVANDER_HOME=str(home), SOVANDER_ENGINE_CONNECT=connect)
    start = time.monotonic()
    out = subprocess.run([sys.executable, "-m", "tests.perf.cold_start", "--child", str(path)], cwd=ROOT,
                         env=env, capture_output=True, text=True, timeout=TIMEOUT + 60)
    line = next((ln for ln in out.stdout.splitlines() if ln.startswith("PROBE ")), None)
    if out.returncode != 0 or line is None:
        raise RuntimeError(f"launch failed ({out.returncode}): {out.stderr[-2000:]}")
    marks = json.loads(line[len("PROBE "):])
    if "first_page" not in marks:
        raise RuntimeError(f"page 1 never drawn: {marks}; {out.stderr[-2000:]}")
    return {name: round(marks[name] - start, 3) for name in MARKS if name in marks}


def engine_start(home: Path, connect: str, folders: list[Path]) -> float:
    from sovander.engine.process import Engine

    if folders:
        evict(folders)
    os.environ["SOVANDER_HOME"] = str(home)  # the engine log lands next to the profile
    engine = Engine(connect=connect, profile=home / PROFILE)
    try:
        start = time.perf_counter()
        engine.start()
        return round(time.perf_counter() - start, 3)
    finally:
        engine.stop()


def summary(runs: list) -> dict:
    if runs and isinstance(runs[0], dict):
        phases = {name: round(statistics.median(run[name] for run in runs), 3)
                  for name in MARKS if name in runs[0]}
        return {"median_s": phases["first_page"], "phases_median_s": phases,
                "runs_s": [r["first_page"] for r in runs]}
    return {"median_s": round(statistics.median(runs), 3), "runs_s": runs}


def measure(files: list[Path], runs: int, evicting: bool, base: Path) -> dict:
    from sovander.engine.process import find_soffice
    from tests.fidelity.engine_harness import connect_string

    soffice = find_soffice()
    if not soffice:
        raise SystemExit("LibreOffice not found (set SOVANDER_SOFFICE or use scripts/dev-env.sh)")
    connect = connect_string(soffice, "cold")
    folders = evict_folders(soffice) if evicting else []
    fresh = iter(range(10_000))

    def new_home(profile: Path | None = None) -> Path:
        home = base / f"home-{next(fresh)}"
        home.mkdir()
        if profile is not None:  # an earlier launch's engine profile, without its render cache
            shutil.copytree(profile, home / PROFILE, symlinks=True)
        return home

    kept = new_home()
    engine_start(kept, connect, [])  # leaves a profile behind for the "later" runs
    profile = kept / PROFILE
    results: dict = {"soffice": soffice, "evict": [str(f) for f in folders], "runs": runs}
    results["engine_start"] = {
        "first": summary([engine_start(new_home(), connect, folders) for _ in range(runs)]),
        "later": summary([engine_start(new_home(profile), connect, folders) for _ in range(runs)]),
    }
    for path in files:
        results[path.name] = {
            "first": summary([launch(path, new_home(), connect, folders) for _ in range(runs)]),
            "later": summary([launch(path, new_home(profile), connect, folders) for _ in range(runs)]),
        }
    return results


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("files", nargs="*", type=Path)
    parser.add_argument("--runs", type=int, default=5)
    parser.add_argument("--evict", action="store_true", help="cold page cache for LibreOffice's files")
    parser.add_argument("--child", help=argparse.SUPPRESS)
    args = parser.parse_args(argv)
    if args.child:
        child(args.child)
        return 0
    files = [f.resolve() for f in (args.files or DEFAULT_FILES) if f.is_file()]
    REPORT.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(dir=REPORT.parent, prefix="cold-") as base:  # the snap cannot read /tmp
        results = measure(files, args.runs, args.evict, Path(base))
    results["when"] = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
    REPORT.write_text(json.dumps(results, indent=1) + "\n", encoding="utf-8")
    print(json.dumps(results, indent=1))
    return 0


if __name__ == "__main__":
    sys.exit(main())
