"""Grade A (Gander JS) and B (LibreOffice -> PDF) renders against the source files themselves.

Truth text comes from each file's own XML (docx/pptx/xlsx/odt/ods), never from either engine.
Run: python3 -I compare.py   (stdlib only; reads results/, writes results/compare.json)
"""
import json
import re
import sys
import unicodedata
import zipfile
from collections import Counter
from pathlib import Path

SPIKE = Path(__file__).resolve().parent
CORPUS = SPIKE / "corpus"
RESULTS = SPIKE / "results"

W = "{http://schemas.openxmlformats.org/wordprocessingml/2006/main}"
A = "{http://schemas.openxmlformats.org/drawingml/2006/main}"
MC_FALLBACK = "{http://schemas.openxmlformats.org/markup-compatibility/2006}Fallback"


def words(text):
    text = unicodedata.normalize("NFKC", text).lower()
    return [w for w in re.findall(r"\w+", text) if len(w) > 1 and not w.isdigit()]


def xml_text(data, tag):
    """Text of every `tag` element, skipping mc:Fallback copies of text boxes."""
    import xml.etree.ElementTree as ET

    root = ET.fromstring(data)
    out = []

    def walk(el):
        if el.tag == MC_FALLBACK:
            return
        if el.tag == tag and el.text:
            out.append(el.text)
        for child in el:
            walk(child)

    walk(root)
    return " ".join(out)


def truth_words(path):
    import xml.etree.ElementTree as ET

    ext = path.suffix.lower()
    try:
        z = zipfile.ZipFile(path)
    except (zipfile.BadZipFile, OSError):
        return None
    names = z.namelist()
    parts = []
    try:
        if ext in (".docx", ".docm", ".dotx"):
            for n in names:
                if re.fullmatch(r"word/(document|header\d*|footer\d*|footnotes|endnotes)\.xml", n):
                    parts.append(xml_text(z.read(n), W + "t"))
        elif ext in (".pptx", ".pptm", ".ppsx", ".potx"):
            for n in names:
                if re.fullmatch(r"ppt/slides/slide\d+\.xml", n):
                    parts.append(xml_text(z.read(n), A + "t"))
        elif ext in (".xlsx", ".xlsm", ".xltx"):
            if "xl/sharedStrings.xml" in names:
                parts.append(xml_text(z.read("xl/sharedStrings.xml"),
                                      "{http://schemas.openxmlformats.org/spreadsheetml/2006/main}t"))
        elif ext in (".odt", ".ods", ".odp"):
            raw = z.read("content.xml").decode("utf8", "replace")
            parts.append(re.sub(r"<[^>]+>", " ", raw))
        else:
            return None
    except (KeyError, OSError, zipfile.BadZipFile, ET.ParseError, RuntimeError, NotImplementedError):
        # Missing parts, damaged or encrypted members, unsupported compression: no truth for this file
        return None
    return Counter(words(" ".join(parts)))


def recall(truth, rendered_text):
    if not truth:
        return None
    got = Counter(words(rendered_text))
    hit = sum(min(c, got[w]) for w, c in truth.items())
    return hit / sum(truth.values())


def load(name):
    p = RESULTS / name
    if not p.exists():
        sys.exit(f"missing {p}")
    data = json.loads(p.read_text())
    recs = data if isinstance(data, list) else data.get("files", data.get("results", []))
    return {Path(r.get("file") or r.get("path") or r.get("name")).name: r for r in recs}


def field(rec, *keys):
    for k in keys:
        if rec.get(k) is not None:
            return rec[k]
    return None


def main():
    a = load("a-gander.json")
    b = load("b-libreoffice.json")
    word_truth = json.loads((RESULTS / "word-truth.json").read_text())
    rows = []
    for folder in ("docx", "doc", "pptx", "xlsx", "gander"):
        for f in sorted((CORPUS / folder).iterdir()):
            if f.suffix.lower() == ".pdf":
                continue
            ra, rb = a.get(f.name, {}), b.get(f.name, {})
            truth = truth_words(f)
            ta = RESULTS / "a-text" / (f.name + ".txt")
            tb = RESULTS / "b-text" / (f.name + ".txt")
            row = {
                "file": f.name,
                "folder": folder,
                "ext": f.suffix.lower(),
                "a_status": field(ra, "status"),
                "b_status": field(rb, "status"),
                "a_pages": field(ra, "count", "pages", "page_count"),
                # Gander splits at Word's own recorded breaks when the file has them: the answer key
                "a_used_word_marks": bool((ra.get("docx") or {}).get("split_on_word_marks")),
                "b_pages": field(rb, "pages", "page_count", "pdf_pages"),
                "word_pages": word_truth.get(f.name, {}).get("pages"),
                "truth_words": sum(truth.values()) if truth else None,
                "a_recall": recall(truth, ta.read_text(errors="replace")) if truth and ta.exists() else None,
                "b_recall": recall(truth, tb.read_text(errors="replace")) if truth and tb.exists() else None,
                "a_missing_boxes": field(ra, "missing_boxes", "missing", "placeholders"),
                "a_filled_cells": (ra.get("xlsx") or {}).get("inline_bg_cells_all_sheets"),
                # White fills are no visible colour, so they do not count as a shown fill
                "b_filled_cells": sum(n for c, n in (rb.get("html_fill_colours") or {}).items()
                                      if c.lower() not in ("#ffffff", "#fff", "white")),
            }
            rows.append(row)
    (RESULTS / "compare.json").write_text(json.dumps(rows, indent=1))
    summarize(rows)


def summarize(rows):
    def pct(n, d):
        return f"{100 * n / d:.0f}% ({n}/{d})" if d else "-"

    print("## Robustness: rendered without error or timeout")
    for folder in ("docx", "doc", "pptx", "xlsx", "gander"):
        rs = [r for r in rows if r["folder"] == folder]
        oka = sum(r["a_status"] == "ok" for r in rs)
        okb = sum(r["b_status"] == "ok" for r in rs)
        print(f"{folder:7} A {pct(oka, len(rs)):>14}   B {pct(okb, len(rs)):>14}")

    print("\n## Pagination vs Word's own page count (Word-saved .docx, both rendered ok)")
    pr = [r for r in rows if r["word_pages"] and r["a_status"] == "ok" and r["b_status"] == "ok"
          and r["a_pages"] is not None and r["b_pages"] is not None]
    groups = (("all", pr),
              ("A had Word's breaks", [r for r in pr if r["a_used_word_marks"]]),
              ("A on its own", [r for r in pr if not r["a_used_word_marks"]]))
    for label, rs in groups:
        for side in ("a", "b"):
            exact = sum(r[f"{side}_pages"] == r["word_pages"] for r in rs)
            near = sum(abs(r[f"{side}_pages"] - r["word_pages"]) <= 1 for r in rs)
            print(f"{label:20} {side.upper()}: exact {pct(exact, len(rs))}, within 1 page {pct(near, len(rs))}")

    print("\n## Spreadsheet cell fills shown (workbooks where B's grid shows fills)")
    fr = [r for r in rows if r["ext"] in (".xlsx", ".xltx", ".ods") and r["b_filled_cells"]]
    print(f"workbooks with fills (per B): {len(fr)}; A showed fills in "
          f"{sum(bool(r['a_filled_cells']) for r in fr)} of them")

    print("\n## Slides: elements A left out")
    sr = [r for r in rows if r["ext"] in (".pptx", ".pptm") and r["a_status"] == "ok"]
    print(f"decks {len(sr)}; decks with missing boxes {sum(bool(r['a_missing_boxes']) for r in sr)}; "
          f"boxes total {sum(r['a_missing_boxes'] or 0 for r in sr)}")

    print("\n## Text recall (share of the file's own words shown), mean over files both rendered")
    for folder in ("docx", "pptx", "xlsx", "gander"):
        rs = [r for r in rows if r["folder"] == folder and r["a_recall"] is not None
              and r["b_recall"] is not None]
        if not rs:
            continue
        ma = sum(r["a_recall"] for r in rs) / len(rs)
        mb = sum(r["b_recall"] for r in rs) / len(rs)
        low_a = sum(r["a_recall"] < 0.9 for r in rs)
        low_b = sum(r["b_recall"] < 0.9 for r in rs)
        print(f"{folder:7} n={len(rs):3}  A {ma:.1%} (files <90%: {low_a})   B {mb:.1%} (files <90%: {low_b})")


if __name__ == "__main__":
    main()
