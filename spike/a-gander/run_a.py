"""
Approach A accuracy pass: every non-PDF file in spike/corpus through Gander's viewer pages.

Each file runs in a child process of its own (fresh server, fresh Chromium), so a file that
hangs or crashes the renderer costs only itself: the parent kills the child's whole process
group after a hard deadline. One browser at a time.

    .venv/bin/python run_a.py                 # whole corpus -> results/a-gander.json
    .venv/bin/python run_a.py --only 52449.docx deck.pptm   # a few, by file name
    .venv/bin/python run_a.py --one <path>    # (internal) one file, prints one JSON line
"""

import argparse
import json
import os
import signal
import subprocess
import sys
import time
from collections import Counter, defaultdict
from pathlib import Path

HERE = Path(__file__).resolve().parent
SPIKE = HERE.parent
CORPUS = SPIKE / "corpus"
RESULTS = SPIKE / "results"
SHOTS = RESULTS / "shots" / "a"
TEXTS = RESULTS / "a-text"
FOLDERS = ("docx", "doc", "pptx", "xlsx", "gander")
HARD_DEADLINE_S = 150  # 60 s render cap + browser launch + measuring + screenshot


def corpus_files():
    files = []
    for folder in FOLDERS:
        for p in sorted((CORPUS / folder).iterdir()):
            if p.is_file() and p.suffix.lower() != ".pdf":
                files.append(p)
    return files


def out_name(path, taken):
    """<file name>, or <folder>__<file name> if two folders hold the same name."""
    name = path.name
    if name in taken and taken[name] != path:
        name = f"{path.parent.name}__{path.name}"
    taken.setdefault(path.name, path)
    return name


def one(path, name):
    """Runs in the child: render one file and return its record."""
    from playwright.sync_api import sync_playwright
    import gander_view as gv
    from server_a import ViewerServer

    path = Path(path)
    ext = path.suffix.lstrip(".").lower()
    html = gv.page_for(path)
    rec = {
        "file": name, "folder": path.parent.name, "format": ext, "bytes": path.stat().st_size,
        "viewer_page": html, "status": None, "error": None, "render_ms": None,
        "count": None, "count_kind": None, "text_chars": 0, "missing": None,
    }
    if html is None:
        rec["status"] = "unsupported"
        rec["error"] = f"no Gander viewer page for .{ext}"
        return rec

    server = ViewerServer()
    errors = []
    try:
        with sync_playwright() as p:
            browser = p.chromium.launch()
            try:
                context, page = gv.new_page(browser, errors)
                state, ms, err = gv.open_and_wait(page, server, path)
                rec["status"], rec["error"] = state, err
                rec["render_ms"] = round(ms)
                if state == "ok":
                    stamp, _ = gv.settled_stamp(page)
                    rec["render_ms_in_page"] = round(stamp) if stamp is not None else None
                    gv.settle(page, html)
                    late = page.evaluate(gv.ERROR_TEXT)
                    if late:
                        # The card went up over a document that had drawn (window.onerror)
                        rec["status"], rec["error"] = "error", "after render: " + late
                        rec["rendered_then_error"] = True
                if state != "timeout":
                    try:
                        m = gv.measure(page, html)
                        text = m.pop("text") or ""
                        all_text = m.pop("all_text", None)
                        rec.update({k: v for k, v in m.items() if k != "refused"})
                        rec["csp_refused"] = m.get("refused", [])
                        rec["text_chars"] = len(text)
                        body = text if all_text is None else (
                            text + "\n\n=== all sheets (vwFindParts) ===\n" + all_text)
                        if all_text is not None:
                            rec["all_sheets_text_chars"] = len(all_text)
                        (TEXTS / f"{name}.txt").write_text(body, encoding="utf-8")
                    except Exception as e:
                        rec["measure_error"] = f"{type(e).__name__}: {str(e)[:200]}"
                try:
                    page.evaluate("() => window.scrollTo(0, 0)")
                    page.screenshot(path=str(SHOTS / f"{name}.png"), timeout=20000)
                    rec["screenshot"] = str((SHOTS / f"{name}.png").relative_to(SPIKE))
                except Exception as e:
                    rec["screenshot_error"] = f"{type(e).__name__}: {str(e)[:200]}"
                context.close()
            finally:
                browser.close()
    finally:
        server.stop()
    rec["page_errors"] = errors[:5]
    return rec


def run_child(path, name):
    cmd = [sys.executable, str(HERE / "run_a.py"), "--one", str(path), "--name", name]
    t0 = time.perf_counter()
    proc = subprocess.Popen(cmd, cwd=HERE, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                            start_new_session=True, text=True)
    try:
        out, err = proc.communicate(timeout=HARD_DEADLINE_S)
    except subprocess.TimeoutExpired:
        os.killpg(proc.pid, signal.SIGKILL)
        proc.communicate()
        return {"file": name, "folder": path.parent.name, "format": path.suffix.lstrip(".").lower(),
                "status": "timeout", "error": f"harness hard deadline {HARD_DEADLINE_S} s (renderer hung)"}
    lines = [l for l in out.splitlines() if l.startswith("{")]
    if proc.returncode != 0 or not lines:
        return {"file": name, "folder": path.parent.name, "format": path.suffix.lstrip(".").lower(),
                "status": "error", "error": f"harness child exit {proc.returncode}: {err.strip()[-400:]}"}
    rec = json.loads(lines[-1])
    rec["child_wall_ms"] = round((time.perf_counter() - t0) * 1000)
    return rec


def summary(records):
    table = defaultdict(Counter)
    for r in records:
        table[r["format"]][r["status"]] += 1
    cols = ["ok", "error", "timeout", "unsupported"]
    lines = [f"{'format':8}" + "".join(f"{c:>12}" for c in cols) + f"{'total':>8}"]
    for fmt in sorted(table):
        c = table[fmt]
        lines.append(f"{fmt:8}" + "".join(f"{c[k]:>12}" for k in cols) + f"{sum(c.values()):>8}")
    tot = Counter(r["status"] for r in records)
    lines.append(f"{'ALL':8}" + "".join(f"{tot[k]:>12}" for k in cols) + f"{len(records):>8}")
    return "\n".join(lines)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--one")
    ap.add_argument("--name")
    ap.add_argument("--only", nargs="*")
    ap.add_argument("--out", default=str(RESULTS / "a-gander.json"))
    a = ap.parse_args()

    SHOTS.mkdir(parents=True, exist_ok=True)
    TEXTS.mkdir(parents=True, exist_ok=True)

    if a.one:
        print(json.dumps(one(a.one, a.name or Path(a.one).name), ensure_ascii=False))
        return

    files = corpus_files()
    taken, named = {}, []
    for f in files:
        named.append((f, out_name(f, taken)))
    if a.only:
        named = [(f, n) for f, n in named if f.name in a.only or n in a.only]

    records = []
    for i, (f, n) in enumerate(named, 1):
        rec = run_child(f, n)
        records.append(rec)
        extra = f" count={rec.get('count')}" if rec.get("count") is not None else ""
        if rec.get("missing"):
            extra += f" missing={rec['missing']}"
        print(f"[{i}/{len(named)}] {rec['status']:8} {rec.get('render_ms') or '':>6} ms "
              f"{n}{extra}" + (f"  ! {rec['error'][:120]}" if rec.get("error") else ""), flush=True)

    Path(a.out).write_text(json.dumps(records, ensure_ascii=False, indent=1), encoding="utf-8")
    print()
    print(summary(records))
    print(f"\nwrote {a.out}")


if __name__ == "__main__":
    main()
