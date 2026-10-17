"""Interface language (Requirement 14): gettext domain "sovander", English source strings, Russian.

Usage: ``from sovander.i18n import _`` and ``_("No matches")``. Text that must change with the
language is read through ``_()`` each time it is shown, and widgets that stay on screen register
``add_listener`` to relabel themselves after ``set_language``.

An installed build ships compiled catalogs in the system locale folder. A source checkout has
``po/*.po`` beside the package; those are compiled here into the cache folder, because gettext
reads only compiled ``.mo`` files and the build tools may not be installed.
"""

from __future__ import annotations

import ast
import gettext as _gettext
import inspect
import io
import logging
import os
import struct
import tempfile
import threading
import weakref
from collections.abc import Callable, Iterable
from dataclasses import dataclass, field
from pathlib import Path

from sovander import paths

log = logging.getLogger(__name__)

DOMAIN = "sovander"
LANGUAGES = ("en", "ru")
SOURCE_LANGUAGE = "en"
PO_DIR = Path(__file__).resolve().parent.parent / "po"

_lock = threading.RLock()
_translation: _gettext.NullTranslations = _gettext.NullTranslations()
_language: str | None = None
_listeners: list[Callable[[], Callable[[str], None] | None]] = []


class _Strong:
    """The same call shape as a weak reference, for plain functions and lambdas."""

    def __init__(self, target):
        self._target = target

    def __call__(self):
        return self._target


# Public API


def _(message: str) -> str:
    """Translate ``message`` into the current language."""
    return _current().gettext(message)


def ngettext(singular: str, plural: str, n: int) -> str:
    return _current().ngettext(singular, plural, n)


def pgettext(context: str, message: str) -> str:
    return _current().pgettext(context, message)


def N_(message: str) -> str:
    """Mark a string for the catalog without translating it now."""
    return message


def language() -> str:
    """The language in use: "en" or "ru"."""
    _current()
    return _language or SOURCE_LANGUAGE


def set_language(lang: str | None) -> str:
    """Switch the interface to ``lang``, or to the system language for None (14.2-14.4).

    Listeners are told only when the language actually changes.
    """
    global _language, _translation
    code = lang or system_language()
    if code not in LANGUAGES:
        raise ValueError(f"unsupported language {lang!r}")
    translation = _load(code)
    with _lock:
        changed = _language is not None and code != _language
        _language, _translation = code, translation
        _listeners[:] = [r for r in _listeners if r() is not None]
        listeners = [r() for r in _listeners]
    if changed:
        for listener in listeners:
            if listener is None:
                continue
            try:
                listener(code)
            except Exception:  # noqa: BLE001 - one broken widget must not stop the others relabelling
                log.exception("language listener failed")
    return code


def add_listener(listener: Callable[[str], None]) -> None:
    """Call ``listener(language)`` after every language change.

    A bound method is held weakly, so a widget that goes away needs no ``remove_listener``.
    """
    ref = weakref.WeakMethod(listener) if inspect.ismethod(listener) else _Strong(listener)
    with _lock:
        _listeners.append(ref)


def remove_listener(listener: Callable[[str], None]) -> None:
    with _lock:
        _listeners[:] = [r for r in _listeners if r() not in (None, listener)]


def system_language(environ: dict[str, str] | None = None) -> str:
    """The system locale's language if it is English or Russian, otherwise English (14.2, 14.3).

    Follows gettext: LANGUAGE is a list of preferences, ignored under the C locale.
    """
    env = os.environ if environ is None else environ
    locale_name = next((env[v] for v in ("LC_ALL", "LC_MESSAGES", "LANG") if env.get(v)), "")
    if _code(locale_name) in ("", "c", "posix"):
        return SOURCE_LANGUAGE
    for name in [*env.get("LANGUAGE", "").split(":"), locale_name]:
        if _code(name) in LANGUAGES:
            return _code(name)
    return SOURCE_LANGUAGE


# Catalogs


def _current() -> _gettext.NullTranslations:
    if _language is None:
        set_language(None)
    return _translation


def _code(locale_name: str) -> str:
    return locale_name.split(".")[0].split("@")[0].split("_")[0].strip().lower()


def _load(code: str) -> _gettext.NullTranslations:
    if code == SOURCE_LANGUAGE:
        return _gettext.NullTranslations()
    po = PO_DIR / f"{code}.po"
    if po.is_file():
        return _from_source(po, code)
    found = _gettext.translation(DOMAIN, languages=[code], fallback=True)
    if type(found) is _gettext.NullTranslations:
        log.warning("no %s catalog installed; showing English", code)
    return found


def _from_source(po: Path, code: str) -> _gettext.NullTranslations:
    """Compile a checkout's .po into the cache folder, once per change of the .po."""
    mo = paths.cache_dir() / "locale" / code / "LC_MESSAGES" / f"{DOMAIN}.mo"
    try:
        if not mo.exists() or mo.stat().st_mtime < po.stat().st_mtime:
            _write_atomic(mo, compile_po(po.read_text(encoding="utf-8")))
        # Read directly: gettext.translation() caches by path and would keep a stale catalog
        with open(mo, "rb") as f:
            return _gettext.GNUTranslations(f)
    except OSError as e:
        log.warning("cannot cache the %s catalog (%s); compiling in memory", code, e)
        return _gettext.GNUTranslations(io.BytesIO(compile_po(po.read_text(encoding="utf-8"))))


def _write_atomic(path: Path, data: bytes) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=path.parent, suffix=".tmp")
    try:
        with os.fdopen(fd, "wb") as f:
            f.write(data)
        os.replace(tmp, path)
    except BaseException:
        Path(tmp).unlink(missing_ok=True)
        raise


# A small msgfmt: enough of the .po format for this project's catalogs


@dataclass
class PoEntry:
    msgid: str = ""
    msgstr: list[str] = field(default_factory=list)
    msgctxt: str | None = None
    msgid_plural: str | None = None
    fuzzy: bool = False


def parse_po(text: str) -> list[PoEntry]:
    """Entries of a .po or .pot file, header included, obsolete (#~) entries left out."""
    entries: list[PoEntry] = []
    entry, target, fuzzy = PoEntry(), None, False

    def flush():
        nonlocal entry, target, fuzzy
        if target is not None:
            entries.append(entry)
        entry, target, fuzzy = PoEntry(), None, False

    for number, raw in enumerate(text.splitlines(), 1):
        line = raw.strip()
        if not line or line.startswith("#~"):
            if not line:
                flush()
            continue
        if line.startswith("#"):
            if entry.msgstr:
                flush()
            if line.startswith("#,") and "fuzzy" in (f.strip() for f in line[2:].split(",")):
                fuzzy = True
            continue
        keyword, _sep, rest = line.partition(" ")
        if line.startswith('"'):
            keyword, rest = None, line
        elif keyword in ("msgctxt", "msgid") and entry.msgstr:
            flush()
        try:
            value = ast.literal_eval(rest.strip())
        except (ValueError, SyntaxError) as e:
            raise ValueError(f"line {number}: bad string {rest!r}") from e
        if not isinstance(value, str):
            raise ValueError(f"line {number}: expected a quoted string")
        entry.fuzzy = fuzzy
        if keyword is None:
            if target is None:
                raise ValueError(f"line {number}: continuation without a keyword")
        elif keyword == "msgctxt":
            target = "msgctxt"
            entry.msgctxt = ""
        elif keyword == "msgid":
            target = "msgid"
        elif keyword == "msgid_plural":
            target = "msgid_plural"
            entry.msgid_plural = ""
        elif keyword == "msgstr" or keyword.startswith("msgstr["):
            target = "msgstr"
            entry.msgstr.append("")
        else:
            raise ValueError(f"line {number}: unknown keyword {keyword!r}")
        if target == "msgstr":
            entry.msgstr[-1] += value
        else:
            setattr(entry, target, (getattr(entry, target) or "") + value)
    flush()
    return entries


def compile_po(text: str) -> bytes:
    """A GNU .mo catalog from .po text; fuzzy and untranslated entries are left out, as msgfmt does."""
    return compile_entries(parse_po(text))


def compile_entries(entries: Iterable[PoEntry]) -> bytes:
    messages: dict[bytes, bytes] = {}
    for e in entries:
        if e.fuzzy or not any(e.msgstr):
            continue
        key = e.msgid if e.msgctxt is None else f"{e.msgctxt}\x04{e.msgid}"
        if e.msgid_plural is not None:
            key += "\0" + e.msgid_plural
        messages[key.encode()] = "\0".join(e.msgstr).encode()
    keys = sorted(messages)
    ids = strs = b""
    offsets = []
    for k in keys:
        offsets.append((len(ids), len(k), len(strs), len(messages[k])))
        ids += k + b"\0"
        strs += messages[k] + b"\0"
    key_start = 7 * 4 + 16 * len(keys)
    value_start = key_start + len(ids)
    table = []
    for id_off, id_len, _off, _len in offsets:
        table += [id_len, id_off + key_start]
    for _id_off, _id_len, str_off, str_len in offsets:
        table += [str_len, str_off + value_start]
    header = struct.pack("<7I", 0x950412DE, 0, len(keys), 7 * 4, 7 * 4 + len(keys) * 8, 0, 0)
    return header + struct.pack(f"<{len(table)}I", *table) + ids + strs
