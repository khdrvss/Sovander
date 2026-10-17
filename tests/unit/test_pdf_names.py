"""Named destinations read from the PDF itself (PyMuPDF 1.19, Ubuntu 22.04, resolves none)."""

try:
    import pymupdf as fitz
except ImportError:  # PyMuPDF before 1.24 has only the old name
    import fitz

from sovander import pdf_names


def pages(count=5):
    doc = fitz.open()
    for i in range(count):
        doc.new_page().insert_text((72, 72), f"page {i + 1}")
    return doc


def obj(doc, text: str) -> int:
    xref = doc.get_new_xref()
    doc.update_object(xref, text)
    return xref


def test_a_name_tree_with_kids():
    doc = pages()
    leaf_a = obj(doc, f"<< /Names [(intro) [{doc[1].xref} 0 R /Fit]] /Limits [(intro) (intro)] >>")
    indirect = obj(doc, f"<< /D [{doc[4].xref} 0 R /XYZ 0 700 0] >>")
    leaf_b = obj(doc, f"<< /Names [(end\\)s) {indirect} 0 R] /Limits [(end\\)s) (end\\)s)] >>")
    root = obj(doc, f"<< /Kids [{leaf_a} 0 R {leaf_b} 0 R] >>")
    doc.xref_set_key(doc.pdf_catalog(), "Names", f"<< /Dests {root} 0 R >>")
    doc = fitz.open(stream=doc.tobytes(), filetype="pdf")
    assert pdf_names.page_of(doc, "intro") == 1
    assert pdf_names.page_of(doc, "end)s") == 4  # an escaped name, a destination dictionary with /D
    assert pdf_names.page_of(doc, "missing") == -1


def test_the_old_dests_dictionary():
    doc = pages()
    dests = obj(doc, f"<< /chapter3 [{doc[2].xref} 0 R /Fit] >>")
    doc.xref_set_key(doc.pdf_catalog(), "Dests", f"{dests} 0 R")
    doc = fitz.open(stream=doc.tobytes(), filetype="pdf")
    assert pdf_names.page_of(doc, "chapter3") == 2


def test_a_name_tree_that_loops_ends():
    """A crafted file: a node that is its own kid must not hang the click."""
    doc = pages()
    node = doc.get_new_xref()
    doc.update_object(node, f"<< /Kids [{node} 0 R] >>")
    doc.xref_set_key(doc.pdf_catalog(), "Names", f"<< /Dests {node} 0 R >>")
    doc = fitz.open(stream=doc.tobytes(), filetype="pdf")
    assert pdf_names.page_of(doc, "anything") == -1


def test_a_document_without_names():
    assert pdf_names.page_of(pages(), "intro") == -1
