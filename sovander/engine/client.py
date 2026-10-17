"""Office files to PDF through the engine, page 1 first (Requirements 2.2–2.5, 2.10, 3.6, 3.8–3.10, 5.7).

Documents travel as bytes in both directions (``private:stream``), so zip entries never touch the disk
(8.3) and the engine never needs to open a user's file by path.
"""

from __future__ import annotations

import codecs
import csv
import functools
import io
import itertools
import re
from collections.abc import Iterator
from pathlib import Path
from typing import Any

from . import process
from .process import Engine, EngineError, is_bridge_failure

TEXT, SHEET, SLIDES, DRAWING = "text", "sheet", "slides", "drawing"
KINDS = (TEXT, SHEET, SLIDES, DRAWING)
_PDF_FILTER = {TEXT: "writer_pdf_Export", SHEET: "calc_pdf_Export", SLIDES: "impress_pdf_Export",
               DRAWING: "draw_pdf_Export"}
_SERVICE_KIND = (("com.sun.star.sheet.SpreadsheetDocument", SHEET),
                 ("com.sun.star.presentation.PresentationDocument", SLIDES),
                 ("com.sun.star.drawing.DrawingDocument", DRAWING),
                 ("com.sun.star.text.TextDocument", TEXT))
CSV_FILTER = "Text - txt - csv (StarCalc)"
_BINARY_MAGIC = (b"PK\x03\x04", b"\xd0\xcf\x11\xe0", b"<?xml", b"{\\rtf", b"\xef\xbb\xbf<?xml")
_PASSWORD_REQUESTS = ("DocumentPasswordRequest", "DocumentPasswordRequest2",
                      "DocumentMSPasswordRequest", "DocumentMSPasswordRequest2")
_WARNING = 0x80000000  # ErrCode warning bit: the file loaded, but not cleanly


class ConversionError(EngineError):
    """The engine could not read this file."""


class DamagedFile(ConversionError):
    """The file is damaged and nothing readable was recovered."""


class NeedsPassword(ConversionError):
    """The file is encrypted; ask the user for the password (3.8)."""


class WrongPassword(NeedsPassword):
    """The password given was wrong; say so and ask again (3.9)."""


class PdfChunk(bytes):
    """PDF bytes plus what the viewer must know about them."""

    final: bool = False  # True: the whole document; False: page 1 only, the full PDF follows
    damaged: bool = False  # the engine repaired the file; show the damaged-file notice (3.10)

    @classmethod
    def of(cls, data: bytes, final: bool, damaged: bool) -> PdfChunk:
        chunk = cls(data)
        chunk.final, chunk.damaged = final, damaged
        return chunk


def convert(source: Path | bytes, kind: str, password: str | None = None,
            engine: Engine | None = None) -> Iterator[PdfChunk]:
    """Yield page 1 as a PDF, then the whole document (5.7); a one-page document yields once.

    Raises NeedsPassword, WrongPassword, DamagedFile, ConversionError, or the engine's
    EngineTimeout / EngineCrashed / EngineUnavailable. A timeout on the full export still leaves
    page 1 with the caller.
    """
    if kind not in KINDS:
        raise ValueError(f"unknown document kind {kind!r}")
    data = read_source(source)
    engine = engine or process.shared()
    state: dict[str, Any] = {}

    def first(eng: Engine) -> bytes:
        doc, handler = open_document(eng, data, kind, password)
        state.update(doc=doc, generation=eng.generation, damaged=handler.damaged)
        state["filter"] = pdf_filter(doc, kind)
        pdf = export(eng, doc, state["filter"], page_range="1")
        state["pages"] = page_count(doc, state["filter"])
        return pdf

    def rest(eng: Engine) -> bytes:
        if eng.generation != state["generation"]:  # another job restarted the engine: reload
            state["doc"], _ = open_document(eng, data, kind, password)
            state["generation"] = eng.generation
        return export(eng, state["doc"], state["filter"])

    try:
        page1 = engine.run(first)
        if state["pages"] == 1:
            yield PdfChunk.of(page1, final=True, damaged=state["damaged"])
            return
        yield PdfChunk.of(page1, final=False, damaged=state["damaged"])
        yield PdfChunk.of(engine.run(rest), final=True, damaged=state["damaged"])
    finally:
        _close_later(engine, state)


def read_source(source: Path | bytes | bytearray | memoryview) -> bytes:
    if isinstance(source, Path):
        return source.read_bytes()
    return bytes(source)


def props(**values: Any) -> tuple:
    """Keyword arguments as a UNO PropertyValue sequence."""
    from com.sun.star.beans import PropertyValue

    out = []
    for name, value in values.items():
        prop = PropertyValue()
        prop.Name, prop.Value = name, value
        out.append(prop)
    return tuple(out)


def open_document(engine: Engine, data: bytes, kind: str, password: str | None = None) -> tuple[Any, Any]:
    """Load bytes hidden, read-only, with macros and link updates off. Returns (document, handler)."""
    import uno

    handler = _handler_class()(password)
    stream = engine.create("com.sun.star.io.SequenceInputStream", uno.ByteSequence(data))
    args = dict(InputStream=stream, Hidden=True, ReadOnly=True,
                MacroExecutionMode=0,  # css.document.MacroExecMode.NEVER_EXECUTE
                UpdateDocMode=0,  # css.document.UpdateDocMode.NO_UPDATE
                InteractionHandler=handler)
    if kind == SHEET and looks_like_csv(data):
        # a stream has no file name, and without one LibreOffice reads CSV as plain text
        args.update(FilterName=CSV_FILTER, FilterOptions=csv_options(data))
    try:
        doc = engine.desktop.loadComponentFromURL("private:stream", "_blank", 0, props(**args))
    except Exception as exc:  # noqa: BLE001 - IOException / IllegalArgumentException mean "not loaded"
        if is_bridge_failure(exc):
            raise
        doc = None
    if doc is None:
        if handler.password_wrong:
            raise WrongPassword("The password is not correct.")
        if handler.password_asked:
            raise NeedsPassword("This file is protected with a password.")
        if handler.damaged:
            raise DamagedFile("This file is damaged and cannot be shown.")
        raise ConversionError("This file could not be opened.")
    return doc, handler


def pdf_filter(doc: Any, kind: str) -> str:
    """The PDF export filter for what the engine actually loaded; content wins over the kind (2.11)."""
    for service, loaded_kind in _SERVICE_KIND:
        try:
            if doc.supportsService(service):
                return _PDF_FILTER[loaded_kind]
        except Exception:  # noqa: BLE001
            break
    return _PDF_FILTER[kind]


def export(engine: Engine, doc: Any, filter_name: str, page_range: str | None = None,
           filter_options: str | None = None) -> bytes:
    """Store the document into memory with the given export filter."""
    import uno

    out = engine.create("com.sun.star.io.SequenceOutputStream")
    args: dict[str, Any] = {"FilterName": filter_name, "OutputStream": out}
    if page_range:
        args["FilterData"] = uno.Any("[]com.sun.star.beans.PropertyValue", props(PageRange=page_range))
    if filter_options:
        args["FilterOptions"] = filter_options
    doc.storeToURL("private:stream", props(**args))
    return out.getWrittenBytes().value


def page_count(doc: Any, filter_name: str) -> int | None:
    """Pages the full PDF will have, when the engine knows it cheaply; None means "export to find out"."""
    try:
        if filter_name in (_PDF_FILTER[SLIDES], _PDF_FILTER[DRAWING]):
            return doc.getDrawPages().getCount()
        if filter_name == _PDF_FILTER[TEXT]:  # exact once page 1 was exported: that formats the layout
            return doc.getCurrentController().getPropertyValue("PageCount")
    except Exception:  # noqa: BLE001 - an unknown count only costs one more export
        return None
    return None


def close_document(doc: Any) -> None:
    try:
        doc.close(True)
    except Exception:  # noqa: BLE001 - CloseVetoException or a dead bridge: dispose instead
        try:
            doc.dispose()
        except Exception:  # noqa: BLE001
            pass


def _close_later(engine: Engine, state: dict[str, Any]) -> None:
    doc = state.get("doc")
    if doc is None or not engine.alive or engine.generation != state.get("generation"):
        return  # never opened, or it died with the engine
    try:
        engine.run(lambda _eng: close_document(doc), timeout=10)
    except EngineError:
        pass


def looks_like_csv(data: bytes) -> bool:
    """Text that is not a known office container: Calc must be told it is CSV."""
    head = data[:4096]
    if head.startswith(_BINARY_MAGIC):
        return False
    if head.startswith((b"\xff\xfe", b"\xfe\xff")):
        return True
    return b"\0" not in head


_CHARSETS = {"utf-16": 65535, "utf-8": 76, "cp1251": 34, "cp1252": 1}  # LibreOffice's RTL_TEXTENCODING_*
_TEXT_COMMA = re.compile(r"\d,\d|, ")  # a decimal comma (3,5) or a comma in prose: not a field separator


def csv_options(data: bytes) -> str:
    """Calc CSV import options: separator, quote, character set, from row 1, detect numbers and dates."""
    sample = data[:65536]
    encoding = csv_encoding(sample)
    separator = csv_separator(sample.decode(encoding, "replace")[:16384])
    # tokens: separator, text quote ("), charset, first row, column formats, language, quoted as text,
    # detect special numbers
    return f"{ord(separator)},34,{_CHARSETS[encoding]},1,,,false,true"


def csv_encoding(sample: bytes) -> str:
    """UTF-16 by its byte order mark, UTF-8 if the bytes decode, else Excel's ANSI code page: Russian
    cp1251 or Western cp1252. Read as cp1251, Russian words are wholly Cyrillic while Western accents
    give mixed words like "Cafй"."""
    if sample.startswith((b"\xff\xfe", b"\xfe\xff")):
        return "utf-16"
    try:
        codecs.getincrementaldecoder("utf-8")().decode(sample, final=False)
        return "utf-8"
    except UnicodeDecodeError:
        pass
    words = [w for w in re.findall(r"[^\W\d_]+", sample.decode("cp1251", "replace")) if not w.isascii()]
    cyrillic = sum(all("Ѐ" <= ch <= "ӿ" for ch in w) for w in words)
    return "cp1251" if cyrillic > len(words) - cyrillic else "cp1252"


def csv_separator(text: str) -> str:
    """The separator that splits the first lines into the same number of fields (at least two).

    csv.Sniffer prefers the comma on a tie, which splits Russian and European Excel files
    ("Город;1000;3,5") at their decimal commas. Here a tab wins when it fits; otherwise the most
    fields win, ties going to ';' over ',' over '|'; and commas that are all decimal or prose
    commas lose to any other separator that fits. A comma when nothing fits.
    """
    if "\n" in text.rstrip("\r\n"):
        text = text[:text.rfind("\n") + 1]  # a sample cut mid-line: its last line is partial
    fits: dict[str, int] = {}
    for sep in ("\t", ";", ",", "|"):
        try:
            rows = itertools.islice(csv.reader(io.StringIO(text), delimiter=sep), 50)
            counts = [len(row) for row in rows if row]
        except csv.Error:
            continue
        common = max(set(counts), key=counts.count, default=0)
        if common >= 2 and counts.count(common) >= 0.8 * len(counts):
            fits[sep] = common
    commas = text.count(",")
    if "," in fits and len(fits) > 1 and len(_TEXT_COMMA.findall(text)) >= 0.9 * commas:
        del fits[","]
    if "\t" in fits:
        return "\t"
    return max(fits, key=fits.get, default=",")  # dict order breaks ties: ';' before ',' before '|'


@functools.cache
def _handler_class() -> type:
    """Interaction handler that answers password, repair and warning questions without a UI."""
    import unohelper
    from com.sun.star.task import XInteractionHandler

    class Handler(unohelper.Base, XInteractionHandler):
        def __init__(self, password: str | None):
            self.password = password
            self.password_asked = self.password_given = self.password_wrong = False
            self.damaged = False

        def handle(self, request: Any) -> None:
            try:
                req = request.getRequest()
            except Exception:  # noqa: BLE001 - unreadable question: refuse it
                req = None
            name = getattr(type(req), "__pyunostruct__", "").rsplit(".", 1)[-1]
            continuations = request.getContinuations()
            if name in _PASSWORD_REQUESTS:
                self.password_asked = True
                if self.password is not None and not self.password_given:
                    # XInteractionPassword or XInteractionPassword2, depending on the format
                    chosen = next((c for c in continuations if hasattr(c, "setPassword")), None)
                    if chosen is not None:
                        chosen.setPassword(self.password)
                        chosen.select()
                        self.password_given = True
                        return
                self.password_wrong = self.password_given  # asked again after our answer
            elif name == "BrokenPackageRequest":
                self.damaged = True  # approving asks LibreOffice to repair the package
                if _select(continuations, "XInteractionApprove"):
                    return
            elif name in ("ErrorCodeRequest", "ErrorCodeRequest2") and (req.ErrCode & 0xFFFFFFFF) & _WARNING:
                self.damaged = True  # loaded with a format warning: show what was read
                if _select(continuations, "XInteractionApprove"):
                    return
            _select(continuations, "XInteractionAbort")

    return Handler


def _pick(continuations: Any, interface: str) -> Any:
    for continuation in continuations:
        try:
            names = {t.typeName.rsplit(".", 1)[-1] for t in continuation.getTypes()}
        except Exception:  # noqa: BLE001
            continue
        if interface in names:
            return continuation
    return None


def _select(continuations: Any, interface: str) -> bool:
    chosen = _pick(continuations, interface)
    if chosen is None:
        return False
    chosen.select()
    return True
