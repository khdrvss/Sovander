"""Approach B timing: time-to-first-page (TTFP) = Office->PDF conversion + PyMuPDF render of page 1.

Modes (run in this order so a cold CLI call never delegates to the warm instance):
  cold     fresh `soffice --convert-to pdf` process per file (snap launcher, profile already initialised)
  cold-p1  same, first page only: --convert-to 'pdf:<filter>:{"PageRange":{"type":"string","value":"1"}}'
  warm     one persistent headless soffice (127.0.0.1:<port>, urp), conversions over UNO
           (loadComponentFromURL hidden/read-only/no macros -> storeToURL pdf -> close)
  warm-p1  same, FilterData PageRange="1"
PDF inputs (.pdf) are timed with PyMuPDF only:
  pdf-cold fresh `python -I` process: import pymupdf + open + render page 1 (wall incl. interpreter start)
  pdf-warm in this process (pymupdf already imported): open + render page 1

Output: one JSON object per line on stdout and appended to --out.
Usage:  .venv/bin/python time_b.py [--modes cold,warm,warm-p1] [--repeat N] [--out FILE] FILE...
"""
import argparse
import json
import os
import platform
import subprocess
import sys
import threading
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from lo_common import (HERE, LO_PROGRAM, PORT, RESULTS, WarmLO, cli_convert, kill_group,  # noqa: E402
                       page1_target, render_first_page, start_warm, uno_env)

ALL_MODES = ["cold", "cold-p1", "warm", "warm-p1"]
WORK = HERE / "time-work"
TIMEOUT = 60.0
RENDER_W = 1000


def emit(rec, fh):
    rec["ts"] = round(time.time(), 3)
    line = json.dumps(rec, ensure_ascii=False)
    print(line, flush=True)
    fh.write(line + "\n")
    fh.flush()


def env_record():
    mem = {}
    try:
        for ln in open("/proc/meminfo"):
            k, v = ln.split(":", 1)
            if k in ("MemAvailable", "SwapFree", "SwapTotal"):
                mem[k + "_MB"] = int(v.split()[0]) // 1024
    except OSError:
        pass
    return {"mode": "env", "host": platform.node(), "loadavg": os.getloadavg(), **mem,
            "lo": "snap " + Path(LO_PROGRAM).resolve().parts[3], "python": sys.version.split()[0]}


# ------------------------------------------------------------------ cold (CLI)
def time_cold(src: Path, page1: bool):
    out_dir = WORK / ("cold-p1" if page1 else "cold")
    target = page1_target(src) if page1 else "pdf"
    status, err, out, conv = cli_convert(src, out_dir, target, TIMEOUT)
    rec = {"status": status, "convert_s": round(conv, 3)}
    if status == "ok":
        pages, rnd = render_first_page(out, None, RENDER_W)
        rec.update(pdf_pages=pages, render_s=round(rnd, 3), ttfp_s=round(conv + rnd, 3))
    else:
        rec["stderr"] = err[-300:]
    return rec


# ------------------------------------------------------------------ warm (UNO)
class Warm:
    def __init__(self, port):
        self.port = port
        self.proc = None
        self.lo = None

    def start(self):
        t0 = time.perf_counter()
        self.proc = start_warm(self.port)
        self.lo = WarmLO(self.port, wait=120)
        return round(time.perf_counter() - t0, 3)

    def stop(self):
        if self.lo:
            self.lo.terminate()
        if self.proc:
            try:
                self.proc.wait(15)
            except subprocess.TimeoutExpired:
                kill_group(self.proc.pid)
                self.proc.wait(5)
        self.proc = self.lo = None

    def kill(self):
        if self.proc:
            kill_group(self.proc.pid)
            try:
                self.proc.wait(10)
            except subprocess.TimeoutExpired:
                pass
        self.proc = self.lo = None

    def convert(self, src: Path, page1: bool):
        out = WORK / ("warm-p1" if page1 else "warm") / (src.name + ".pdf")
        out.parent.mkdir(parents=True, exist_ok=True)
        if out.exists():
            out.unlink()
        box = {}

        def job():
            try:
                box["phases"] = self.lo.convert_pdf(src, out, page1=page1)
            except Exception as e:  # noqa: BLE001 - report any UNO failure as data
                box["err"] = f"{type(e).__name__}: {e}"

        t0 = time.perf_counter()
        th = threading.Thread(target=job, daemon=True)
        th.start()
        th.join(TIMEOUT)
        conv = time.perf_counter() - t0
        if th.is_alive():
            self.kill()  # hung document: kill only our instance, caller restarts it
            return {"status": "timeout", "convert_s": round(conv, 3)}, True
        if "err" in box or not (out.exists() and out.stat().st_size > 0):
            dead = self.proc.poll() is not None or any(s in box.get("err", "") for s in ("Disposed", "bridge"))
            return {"status": "error", "convert_s": round(conv, 3),
                    "stderr": box.get("err", "no output")[-300:]}, dead
        pages, rnd = render_first_page(out, None, RENDER_W)
        rec = {"status": "ok", "convert_s": round(conv, 3), "pdf_pages": pages, "render_s": round(rnd, 3),
               "ttfp_s": round(conv + rnd, 3)}
        rec.update({k: round(v, 3) for k, v in box["phases"].items()})
        return rec, False


# ------------------------------------------------------------------ PDF only
PDF_CHILD = r"""
import sys, time, json
t0 = time.perf_counter()
import pymupdf
t1 = time.perf_counter()
d = pymupdf.open(sys.argv[1]); n = d.page_count; p = d[0]; z = %d / max(p.rect.width, 1)
p.get_pixmap(matrix=pymupdf.Matrix(z, z), alpha=False)
t2 = time.perf_counter()
print(json.dumps({"import_s": t1 - t0, "open_render_s": t2 - t1, "pages": n}))
""" % RENDER_W


def time_pdf_cold(src: Path):
    t0 = time.perf_counter()
    p = subprocess.run([sys.executable, "-I", "-c", PDF_CHILD, str(src)], capture_output=True, text=True,
                       timeout=TIMEOUT, cwd=str(HERE))
    wall = time.perf_counter() - t0
    if p.returncode != 0:
        return {"status": "error", "ttfp_s": round(wall, 3), "stderr": p.stderr[-300:]}
    j = json.loads(p.stdout.strip().splitlines()[-1])
    return {"status": "ok", "ttfp_s": round(wall, 3), "pdf_pages": j["pages"],
            "import_s": round(j["import_s"], 3), "render_s": round(j["open_render_s"], 3)}


def time_pdf_warm(src: Path):
    pages, rnd = render_first_page(src, None, RENDER_W)
    return {"status": "ok", "ttfp_s": round(rnd, 3), "render_s": round(rnd, 3), "pdf_pages": pages}


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("files", nargs="+")
    ap.add_argument("--modes", default="cold,warm,warm-p1", help="subset of " + ",".join(ALL_MODES))
    ap.add_argument("--repeat", type=int, default=1, help="runs per file per mode")
    ap.add_argument("--port", type=int, default=PORT)
    ap.add_argument("--out", default=str(RESULTS / "b-timing.jsonl"))
    a = ap.parse_args()
    modes = [m for m in ALL_MODES if m in a.modes.split(",")]
    files = [Path(f).resolve() for f in a.files]
    office = [f for f in files if f.suffix.lower() != ".pdf"]
    pdfs = [f for f in files if f.suffix.lower() == ".pdf"]

    # warm modes need pyuno from the snap: LD_LIBRARY_PATH must be set before the interpreter starts
    if office and any(m.startswith("warm") for m in modes) and "URE_BOOTSTRAP" not in os.environ:
        os.execve(sys.executable, [sys.executable, __file__] + sys.argv[1:], uno_env())
    sys.path.insert(0, LO_PROGRAM)

    import pymupdf  # noqa: F401  (import cost is part of process start, not of TTFP in warm mode)

    fh = open(a.out, "a")
    emit(env_record(), fh)

    for f in pdfs:  # PDF baseline (no LibreOffice involved)
        for r in range(a.repeat):
            emit({"mode": "pdf-cold", "file": f.name, "rep": r, **time_pdf_cold(f)}, fh)
        for r in range(a.repeat):
            emit({"mode": "pdf-warm", "file": f.name, "rep": r, **time_pdf_warm(f)}, fh)

    for mode in ("cold", "cold-p1"):
        if mode in modes:
            for f in office:
                for r in range(a.repeat):
                    emit({"mode": mode, "file": f.name, "rep": r, **time_cold(f, mode == "cold-p1")}, fh)

    warm_modes = [m for m in modes if m.startswith("warm")]
    if office and warm_modes:
        w = Warm(a.port)
        emit({"mode": "warm-start", "startup_to_uno_ready_s": w.start()}, fh)
        try:
            for mode in warm_modes:
                for f in office:
                    for r in range(a.repeat):
                        rec, dead = w.convert(f, mode == "warm-p1")
                        emit({"mode": mode, "file": f.name, "rep": r, **rec}, fh)
                        if dead or w.proc is None:
                            w.kill()
                            emit({"mode": "warm-restart", "startup_to_uno_ready_s": w.start()}, fh)
        finally:
            w.stop()
    fh.close()


if __name__ == "__main__":
    main()
