"""The open pipeline's worker side: reading files and engine work, off the main thread.

Jobs talk to their document page only through ``post(method, *values)``, which calls that method
on the main thread if the page still exists and still waits for this attempt, and stop early when
``alive()`` turns false. Also here: what a document is (Source) and why it cannot be shown.
"""

from __future__ import annotations

import contextlib
import functools
import logging
import os
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path

from gi.repository import GLib

from sovander import archive, i18n
from sovander.cache import FULL, PAGE1, content_key
from sovander.engine import client, macros
from sovander.engine.process import EngineCrashed, EngineError, EngineTimeout
from sovander.i18n import N_, _
from sovander.router import Kind
from sovander.views.image_view import ImageError

log = logging.getLogger(__name__)

ENGINE_KINDS = {
    Kind.WORD: client.TEXT, Kind.WORD_LEGACY: client.TEXT, Kind.ODT: client.TEXT, Kind.RTF: client.TEXT,
    Kind.SLIDES: client.SLIDES, Kind.DRAWING: client.DRAWING,
}
DAMAGED_PART = "damaged"  # cache marker: the stored render came from a repaired file (3.10)

MACROS_NOTE = N_("This document contains macros. They were not run.")
COULD_NOT_OPEN = N_("This file could not be opened")
# Why a file cannot be shown, by exception type; the first match wins, so subclasses come first
REASONS = (
    (FileNotFoundError, N_("The file does not exist")),
    (PermissionError, N_("No permission to read the file")),
    (client.DamagedFile, N_("This file is damaged")),
    (EngineTimeout, N_("The document took too long to prepare and was stopped")),
    (EngineCrashed, N_("The document engine stopped while opening this file")),
    (client.ConversionError, COULD_NOT_OPEN),
    (archive.TooLarge, N_("Too large to open safely")),
    (archive.Damaged, N_("This file is damaged")),
    (archive.Unsupported, N_("This format is not supported")),
)


@dataclass(frozen=True, eq=False)
class Source:
    """A document to open: a file on disk, or bytes held in memory such as a zip entry (8.3)."""

    name: str
    path: Path | None = None
    data: bytes | None = field(default=None, repr=False)

    @classmethod
    def from_path(cls, path: str | os.PathLike) -> Source:
        path = Path(path)
        return cls(path.name or str(path), path)

    @classmethod
    def from_bytes(cls, name: str, data: bytes) -> Source:
        return cls(name, None, bytes(data))

    def read(self) -> bytes:
        if self.data is not None:
            return self.data
        if self.path is None:
            raise FileNotFoundError(self.name)
        return self.path.read_bytes()


class NotARegularFile(OSError):
    """A folder, pipe or device: opening a pipe would wait for a writer forever."""


def reason_for(exc: BaseException) -> Callable[[], str]:
    """The reason to show for ``exc``, translated each time it is shown (1.6)."""
    for kind, message in REASONS:
        if isinstance(exc, kind):
            return functools.partial(_, message)
    if isinstance(exc, ImageError):
        return exc.__str__  # a sentence in the current language
    if isinstance(exc, OSError) and not isinstance(exc, NotARegularFile) and exc.strerror:
        return lambda: exc.strerror
    return functools.partial(_, COULD_NOT_OPEN)


def run_job(job, post, alive, args) -> None:
    """Thread body: every failure becomes a message on that one document (10.5)."""
    try:
        job(post, alive, *args)
    except Exception as exc:  # noqa: BLE001
        if not isinstance(exc, EngineError | OSError | archive.ArchiveError | ImageError):
            log.exception("opening failed")
        post("_fail", exc)


def deliver(ref, token, method, values):
    """Idle callback on the main thread: hand a job's result to its page, if it still wants it."""
    page = ref()
    if page is not None and page._token == token:
        try:
            getattr(page, method)(*values)
        except Exception as exc:  # noqa: BLE001 - a view that cannot take the result: say so on the page
            if not isinstance(exc, ImageError):
                log.exception("showing %s failed", page.source.name)
            page._fail(exc)
    return GLib.SOURCE_REMOVE


def office_job(post, alive, source: Source, kind: Kind, password: str | None, context) -> None:
    """A cached render, else the engine: page 1 first, then the whole document (5.7)."""
    data = source.read()
    if macros.has_macros(data):
        post("_add_note", "macros", MACROS_NOTE)
    engine = context.engine
    # A render of a password-protected file is never stored: the cache would keep it readable
    key = content_key(data) if password is None else None
    cache = context.cache(engine) if key else None
    shown_page1 = False
    if cache is not None:
        damaged = cache.get(key, DAMAGED_PART) is not None
        full = cache.get(key, FULL)
        if full is not None:
            post("_show_pdf", full, True, damaged)
            return
        page1 = cache.get(key, PAGE1)
        if page1 is not None:
            post("_show_pdf", page1, False, damaged)
            shown_page1 = True
    with contextlib.closing(client.convert(data, ENGINE_KINDS[kind], password, engine)) as chunks:
        for chunk in chunks:
            if not alive():
                return  # closing the generator closes the document in the engine
            if key is not None:
                cache = context.cache(engine)  # the engine runs now: keyed by its real version
                if cache is not None:
                    cache.put(key, FULL if chunk.final else PAGE1, chunk)
                    if chunk.damaged:
                        cache.put(key, DAMAGED_PART, b"1")
            if chunk.final or not shown_page1:
                post("_show_pdf", bytes(chunk), chunk.final, chunk.damaged)


def sheet_job(post, alive, source: Source, password: str | None, context) -> None:
    """A workbook's grids from the engine (4); a cell's formula is asked later, on demand (4.5)."""
    from sovander.engine import sheets

    data = source.read()
    if macros.has_macros(data):
        post("_add_note", "macros", MACROS_NOTE)
    engine = context.engine
    book = sheets.workbook(data, password, engine)
    post("_show_workbook", book, functools.partial(sheets.formula, data, password=password, engine=engine))


def read_job(post, alive, source: Source, kind: Kind) -> None:
    """Images, books and Markdown as bytes; a zip as its index, entries read later (8.3)."""
    if kind is Kind.ZIP:
        where = source.data if source.data is not None else source.path
        post("_show_archive", archive.Archive(where, lang=i18n.language()))
    else:
        post("_show_bytes", kind, source.read())
