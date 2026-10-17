"""Truth words from a file's own XML, and how many of them a render shows (from spike/compare.py).

Neither engine is consulted for the truth, so the grade cannot flatter the engine being graded.
Kept as close to the spike's grader as possible: a floor taken from the spike only means
something if it is measured the same way.
"""

from __future__ import annotations

import re
import unicodedata
import zipfile
from collections import Counter
from pathlib import Path
from xml.etree import ElementTree as ET

W = "{http://schemas.openxmlformats.org/wordprocessingml/2006/main}"
A = "{http://schemas.openxmlformats.org/drawingml/2006/main}"
S = "{http://schemas.openxmlformats.org/spreadsheetml/2006/main}"
MC_FALLBACK = "{http://schemas.openxmlformats.org/markup-compatibility/2006}Fallback"
_WORD_PARTS = re.compile(r"word/(document|header\d*|footer\d*|footnotes|endnotes)\.xml")
_SLIDE_PARTS = re.compile(r"ppt/slides/slide\d+\.xml")


def words(text: str) -> list[str]:
    """Lower-case words of two or more characters, numbers left out (page numbers, dates)."""
    text = unicodedata.normalize("NFKC", text).lower()
    return [w for w in re.findall(r"\w+", text) if len(w) > 1 and not w.isdigit()]


def xml_text(data: bytes, tag: str) -> str:
    """Text of every `tag` element, skipping mc:Fallback copies of text boxes (counted twice otherwise)."""
    out: list[str] = []

    def walk(el: ET.Element) -> None:
        if el.tag == MC_FALLBACK:
            return
        if el.tag == tag and el.text:
            out.append(el.text)
        for child in el:
            walk(child)

    walk(ET.fromstring(data))
    return " ".join(out)


def truth_words(path: Path) -> Counter | None:
    """Word counts of the file's own text, or None when the file has no readable text parts."""
    ext = path.suffix.lower()
    try:
        z = zipfile.ZipFile(path)
    except (zipfile.BadZipFile, OSError):
        return None
    parts: list[str] = []
    try:
        names = z.namelist()
        if ext in (".docx", ".docm", ".dotx"):
            parts = [xml_text(z.read(n), W + "t") for n in names if _WORD_PARTS.fullmatch(n)]
        elif ext in (".pptx", ".pptm", ".ppsx", ".potx"):
            parts = [xml_text(z.read(n), A + "t") for n in names if _SLIDE_PARTS.fullmatch(n)]
        elif ext in (".xlsx", ".xlsm", ".xltx"):
            if "xl/sharedStrings.xml" in names:
                parts = [xml_text(z.read("xl/sharedStrings.xml"), S + "t")]
        elif ext in (".odt", ".ods", ".odp"):
            parts = [re.sub(r"<[^>]+>", " ", z.read("content.xml").decode("utf8", "replace"))]
        else:
            return None
    except (KeyError, OSError, zipfile.BadZipFile, ET.ParseError, RuntimeError, NotImplementedError):
        return None  # missing parts, damaged or encrypted members: no truth for this file
    finally:
        z.close()
    counts = Counter(words(" ".join(parts)))
    return counts or None


def recall(truth: Counter, rendered_text: str) -> float:
    """Share of the truth's words (with repeats) that the rendered text shows."""
    got = Counter(words(rendered_text))
    hit = sum(min(count, got[word]) for word, count in truth.items())
    return hit / sum(truth.values())
