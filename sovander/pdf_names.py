"""A PDF's named destinations, read from the file itself.

PyMuPDF's resolve_link answers -1 for every name in 1.19 (Ubuntu 22.04's python3-fitz), so links to
"#chapter2" went nowhere there. This looks the name up in the document's name tree (/Names /Dests,
PDF 1.2+) or the older /Dests dictionary, through xref_get_key, which every supported PyMuPDF has. The
file is untrusted: the walk is bounded in depth and visits each node once.
"""

from __future__ import annotations

import re

MAX_DEPTH = 32  # a name tree is balanced; deeper is a loop or a crafted file
_PAIR = re.compile(r"\(((?:\\.|[^\\)])*)\)\s*(?:\[\s*(\d+)\s+\d+\s+R|(\d+)\s+\d+\s+R)")
_REF = re.compile(r"(\d+)\s+\d+\s+R")


def page_of(doc, name: str) -> int:
    """The page index a named destination leads to, or -1."""
    try:
        catalog = doc.pdf_catalog()
        kind, value = doc.xref_get_key(catalog, "Names/Dests")
        if kind == "xref":
            target = _in_tree(doc, int(value.split()[0]), name, 0, set())
        else:
            kind, value = doc.xref_get_key(catalog, "Dests")  # PDF 1.1: a dictionary of names
            dests = int(value.split()[0]) if kind == "xref" else 0
            target = _destination(doc, doc.xref_get_key(dests, name)[1]) if dests else None
    except (RuntimeError, ValueError, IndexError):
        return -1
    if target is None:
        return -1
    for index in range(doc.page_count):
        if doc.page_xref(index) == target:
            return index
    return -1


def _in_tree(doc, node: int, name: str, depth: int, seen: set[int]) -> int | None:
    """The page object of ``name`` in the name tree below ``node``."""
    if depth > MAX_DEPTH or node in seen:
        return None
    seen.add(node)
    kind, names = doc.xref_get_key(node, "Names")
    if kind == "array":
        for match in _PAIR.finditer(names):
            if _unescape(match.group(1)) == name:
                return int(match.group(2)) if match.group(2) else _destination(doc, f"{match.group(3)} 0 R")
    kind, kids = doc.xref_get_key(node, "Kids")
    if kind == "array":
        for kid in _REF.findall(kids):
            found = _in_tree(doc, int(kid), name, depth + 1, seen)
            if found is not None:
                return found
    return None


def _destination(doc, value: str) -> int | None:
    """The page object of a destination: an array [page …], or a reference to one or to a dict with /D."""
    ref = _REF.search(value or "")
    if ref is None:
        return None
    if value.lstrip().startswith("["):
        return int(ref.group(1))
    target = doc.xref_object(int(ref.group(1)), compressed=True)
    inner = _REF.search(target)
    return int(inner.group(1)) if inner else None


def _unescape(text: str) -> str:
    return re.sub(r"\\(.)", r"\1", text)
