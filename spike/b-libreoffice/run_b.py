"""Approach B robustness/fidelity pass: Office -> PDF (LibreOffice headless, cold CLI) -> PyMuPDF.

Per file: status (ok/error/timeout + stderr), PDF page count, extracted text (results/b-text/),
page-1 PNG ~1000 px wide (results/shots/b/). Spreadsheets also go through Calc's HTML export
("html:HTML (StarCalc)") to count filled cells (bgcolor attr / background style) and sheets.

Writes results/b-libreoffice.json after every file (partial results survive a crash).
Usage: .venv/bin/python run_b.py [--only SUBSTR] [--limit N] [--folders docx,doc,...] [--resume]
"""
import argparse
import json
import re
import shutil
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from lo_common import CORPUS, HERE, RESULTS, SHEET_EXT, cli_convert, render_first_page, write_json  # noqa: E402

FOLDERS = ["docx", "doc", "pptx", "xlsx", "gander"]
OUT_JSON = RESULTS / "b-libreoffice.json"
TEXT_DIR = RESULTS / "b-text"
SHOT_DIR = RESULTS / "shots" / "b"
WORK = HERE / "work"  # converter output (non-hidden, inside spike: snap confinement)
TIMEOUT = 60.0

CELL_TAG = re.compile(r"<t[dh]\b[^>]*>", re.I)
HAS_FILL = re.compile(r"\bbgcolor\s*=|style\s*=\s*\"[^\"]*background", re.I)
FILL_VAL = re.compile(r"bgcolor\s*=\s*\"?([^\"\s>]+)|background(?:-color)?\s*:\s*([^;\"]+)", re.I)
SHEET_H1 = re.compile(r"<h1>\s*Sheet\s+\d+:", re.I)


def out_name(src: Path) -> str:
    return src.name  # unique across corpus (checked in main)


def sheet_html_stats(src: Path):
    d = WORK / "html" / src.name
    shutil.rmtree(d, ignore_errors=True)
    status, err, out, secs = cli_convert(src, d, "html:HTML (StarCalc)", TIMEOUT)
    rec = {"html_status": status, "html_convert_s": round(secs, 3)}
    if status != "ok":
        rec["html_stderr"] = err[-500:]
        return rec
    html = out.read_text(errors="replace")
    cells = CELL_TAG.findall(html)
    filled = [c for c in cells if HAS_FILL.search(c)]
    colours = {}
    for c in filled:
        m = FILL_VAL.search(c)
        if m:
            v = (m.group(1) or m.group(2) or "").strip().lower()
            colours[v] = colours.get(v, 0) + 1
    sheets = len(SHEET_H1.findall(html)) or len(re.findall(r"<table\b", html, re.I))
    rec.update({"html_cells": len(cells), "html_filled_cells": len(filled),
                "html_fill_colours": dict(sorted(colours.items(), key=lambda kv: -kv[1])[:10]),
                "html_sheets": sheets, "html_bytes": len(html)})
    return rec


def process(src: Path, folder: str):
    name = out_name(src)
    rec = {"file": f"{folder}/{src.name}", "folder": folder, "ext": src.suffix.lower().lstrip(".")}
    pdf_dir = WORK / "pdf" / folder / src.name  # one dir per file: soffice names output by stem
    shutil.rmtree(pdf_dir, ignore_errors=True)
    status, err, out, secs = cli_convert(src, pdf_dir, "pdf", TIMEOUT)
    rec.update({"status": status, "convert_s": round(secs, 3)})
    if err:
        rec["stderr"] = err[-800:]
    if status == "ok":
        try:
            import pymupdf
            png = SHOT_DIR / (name + ".png")
            pages, rsecs = render_first_page(out, png, 1000)
            rec.update({"pdf_pages": pages, "render_p1_s": round(rsecs, 3),
                        "png": str(png.relative_to(RESULTS)) if pages else None,
                        "pdf_bytes": out.stat().st_size})
            doc = pymupdf.open(str(out))
            text = "\n\f".join(p.get_text() for p in doc)
            doc.close()
            TEXT_DIR.mkdir(parents=True, exist_ok=True)
            (TEXT_DIR / (name + ".txt")).write_text(text)
            rec.update({"text_chars": len(text), "text_words": len(text.split())})
        except Exception as e:  # broken PDF from LO is a B failure too
            rec.update({"status": "error", "stderr": f"pymupdf: {type(e).__name__}: {e}"[:800]})
    if src.suffix.lower() in SHEET_EXT:
        rec.update(sheet_html_stats(src))
    return rec


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--only", help="substring filter on folder/name")
    ap.add_argument("--limit", type=int)
    ap.add_argument("--folders", default=",".join(FOLDERS))
    ap.add_argument("--resume", action="store_true", help="keep records already in the JSON")
    a = ap.parse_args()

    files = []
    for folder in a.folders.split(","):
        for p in sorted((CORPUS / folder).iterdir()):
            if p.is_file() and p.suffix.lower() != ".pdf":
                files.append((folder, p))
    names = [p.name for _, p in files]
    dup = {n for n in names if names.count(n) > 1}
    if dup:
        sys.exit(f"duplicate file names across folders: {sorted(dup)}")
    if a.only:
        files = [(f, p) for f, p in files if a.only in f"{f}/{p.name}"]
    if a.limit:
        files = files[: a.limit]

    records = []
    if a.resume and OUT_JSON.exists():
        records = json.loads(OUT_JSON.read_text())
    done = {r["file"] for r in records}
    t_all = time.perf_counter()
    for i, (folder, p) in enumerate(files, 1):
        key = f"{folder}/{p.name}"
        if key in done:
            continue
        rec = process(p, folder)
        records = [r for r in records if r["file"] != key] + [rec]
        write_json(OUT_JSON, records)
        extra = f" fills={rec.get('html_filled_cells')} sheets={rec.get('html_sheets')}" if "html_status" in rec else ""
        print(f"[{i}/{len(files)}] {rec['status']:7} {rec['convert_s']:6.1f}s pages={rec.get('pdf_pages')} {key}{extra}", flush=True)

    # summary per format
    summ = {}
    for r in records:
        s = summ.setdefault(r["ext"], {"ok": 0, "error": 0, "timeout": 0})
        s[r["status"]] = s.get(r["status"], 0) + 1
    print(f"\n{'format':8} {'ok':>4} {'error':>6} {'timeout':>8}")
    for ext, s in sorted(summ.items()):
        print(f"{ext:8} {s['ok']:4} {s['error']:6} {s['timeout']:8}")
    tot = {k: sum(s[k] for s in summ.values()) for k in ("ok", "error", "timeout")}
    print(f"{'TOTAL':8} {tot['ok']:4} {tot['error']:6} {tot['timeout']:8}   ({time.perf_counter() - t_all:.0f}s this run)")


if __name__ == "__main__":
    main()
