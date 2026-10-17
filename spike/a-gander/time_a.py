"""
Approach A timing: time to first page of Gander's viewer pages in headless Chromium.

    .venv/bin/python time_a.py FILE [FILE ...] [--cold 3] [--warm 3] [--out results/a-timing.jsonl]

For each file, prints one JSON line per measurement:

  cold  fresh Chromium launch + new context/page + navigation until the rendered signal.
        total_ms = launch_ms + page_ms; the Playwright driver's own start is excluded.
  warm  Chromium already running (launched once per file and warmed by one unmeasured
        render of the same file); each measurement is a NEW context + page, so no HTTP or
        code cache is shared with the warm-up. total_ms = new context/page + navigation
        until the signal.

nav_to_signal_ms is what the harness saw (navigation start to the signal, polled every
50 ms); in_page_ms is the page's own stamp (performance.now() when #vw-status went), the
most precise number and free of the harness's polling and CDP round trips.

"Rendered signal" = the status card goes (vwStatusDone), which Gander's pages do only once
the whole document is drawn: for docx that is after Word pagination and any second
drawing, for pptx once every slide exists, for xlsx once the first sheet's first piece is
in. So "first page" here is "document ready", the same moment the app's reader sees it.
"""

import argparse
import json
import sys
import time
from pathlib import Path

from playwright.sync_api import sync_playwright

import gander_view as gv
from server_a import ViewerServer


def once(browser, server, path, cap_ms):
    t0 = time.perf_counter()
    context, page = gv.new_page(browser)
    t1 = time.perf_counter()
    state, nav_ms, err = gv.open_and_wait(page, server, path, cap_ms)
    t2 = time.perf_counter()
    stamp = None
    if state != "timeout":
        try:
            stamp, _ = gv.settled_stamp(page)
        except Exception:
            pass
    context.close()
    return {
        "status": state, "error": err,
        "new_page_ms": round((t1 - t0) * 1000, 1),
        "nav_to_signal_ms": round(nav_ms, 1),
        "in_page_ms": round(stamp, 1) if stamp is not None else None,
        "page_ms": round((t2 - t0) * 1000, 1),
    }


def emit(rec, out):
    line = json.dumps(rec, ensure_ascii=False)
    print(line, flush=True)
    if out:
        with open(out, "a", encoding="utf-8") as f:
            f.write(line + "\n")


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("files", nargs="+")
    ap.add_argument("--cold", type=int, default=3)
    ap.add_argument("--warm", type=int, default=3)
    ap.add_argument("--cap", type=int, default=gv.CAP_MS, help="render cap in ms")
    ap.add_argument("--out", help="also append the JSON lines to this file")
    a = ap.parse_args()

    server = ViewerServer()
    try:
        with sync_playwright() as p:
            for name in a.files:
                path = Path(name).resolve()
                if gv.page_for(path) is None:
                    emit({"file": path.name, "mode": "-", "status": "unsupported"}, a.out)
                    continue
                base = {"file": path.name, "format": path.suffix.lstrip(".").lower(),
                        "viewer_page": gv.page_for(path), "bytes": path.stat().st_size}

                for rep in range(1, a.cold + 1):
                    t0 = time.perf_counter()
                    browser = p.chromium.launch()
                    launch_ms = (time.perf_counter() - t0) * 1000
                    try:
                        r = once(browser, server, path, a.cap)
                    finally:
                        browser.close()
                    emit({**base, "mode": "cold", "rep": rep, "launch_ms": round(launch_ms, 1),
                          **r, "total_ms": round(launch_ms + r["page_ms"], 1)}, a.out)

                if a.warm:
                    browser = p.chromium.launch()
                    try:
                        once(browser, server, path, a.cap)  # warm-up, not reported
                        for rep in range(1, a.warm + 1):
                            r = once(browser, server, path, a.cap)
                            emit({**base, "mode": "warm", "rep": rep, **r,
                                  "total_ms": r["page_ms"]}, a.out)
                    finally:
                        browser.close()
    finally:
        server.stop()


if __name__ == "__main__":
    sys.exit(main())
