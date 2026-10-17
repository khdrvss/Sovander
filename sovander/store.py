"""Recents, reading positions and settings (Requirements 9.10, 11).

Recents and positions are JSON Lines, one entry per line, so a damaged line costs that entry and
never the whole list (Gander parsed its list in one block, and one bad entry emptied it). Every
write replaces the whole file atomically. A file that had anything unreadable in it is copied to
``<name>.bak`` before it is next written, so nothing is lost without a copy.
"""

from __future__ import annotations

import hashlib
import json
import logging
import os
import shutil
import tempfile
import threading
from collections.abc import Callable
from dataclasses import asdict, dataclass, field, fields, replace
from pathlib import Path
from time import time as _now
from urllib.parse import unquote, urlparse

from sovander import paths

log = logging.getLogger(__name__)

RECENTS_MAX = 50
POSITIONS_MAX = 500
HEAD_BYTES = 64 * 1024

RECENTS_FILE = "recents.jsonl"
POSITIONS_FILE = "positions.jsonl"
SETTINGS_FILE = "settings.json"

LANGUAGES = (None, "en", "ru")
THEMES = ("system", "light", "dark")


@dataclass(frozen=True)
class Recent:
    uri: str
    display_name: str
    kind: str
    opened_at: float
    thumb_key: str | None = None
    doc_key: str | None = None
    # Worked out when read, never stored (11.4)
    missing: bool = field(default=False, compare=False)


@dataclass(frozen=True)
class Position:
    doc_key: str
    page: int
    offset_y: float = 0.0
    zoom: float = 1.0
    updated_at: float = 0.0


@dataclass(frozen=True)
class Settings:
    language: str | None = None  # None follows the system locale
    theme: str = "system"
    night_mode: bool = False
    history_enabled: bool = True
    text_page_mb: int = 5


@dataclass(frozen=True)
class Forgotten:
    """What was just forgotten, for the thumbnail and render cache to purge (11.3)."""

    uris: tuple[str, ...]
    doc_keys: tuple[str, ...]
    thumb_keys: tuple[str, ...]
    everything: bool = False


def doc_key(source: Path | str | bytes) -> str | None:
    """Key a document by what it holds: sha256 of its first 64 KB and its size (Gander's scheme).

    The same file reached by another path, or renamed, keeps its key. Returns None when unreadable.
    """
    if isinstance(source, bytes):
        head, size = source[:HEAD_BYTES], len(source)
    else:
        try:
            with open(source, "rb") as f:
                size = os.fstat(f.fileno()).st_size
                head = f.read(HEAD_BYTES)
        except OSError:
            return None
    if not head:
        return None
    digest = hashlib.sha256(head)
    digest.update(str(size).encode())
    return digest.hexdigest()[:32]


def local_path(uri: str) -> Path | None:
    """The file behind a file: URI or an absolute path, or None for anything else."""
    if uri.startswith("/"):
        return Path(uri)
    parsed = urlparse(uri)
    if parsed.scheme == "file" and parsed.netloc in ("", "localhost"):
        return Path(unquote(parsed.path))
    return None


class Store:
    """Recents, positions and settings in one data folder; safe to share between threads."""

    def __init__(self, folder: Path | None = None):
        self._folder = Path(folder) if folder else paths.data_dir()
        self._lock = threading.RLock()
        self._recents: list[Recent] | None = None
        self._positions: dict[str, Position] | None = None
        self._settings: Settings | None = None
        self._forget_hooks: list[Callable[[Forgotten], None]] = []

    # Settings

    @property
    def settings(self) -> Settings:
        with self._lock:
            if self._settings is None:
                self._settings = self._load_settings()
            return self._settings

    def update_settings(self, **changes) -> Settings:
        """Change some settings; an unknown name or a bad value raises ValueError."""
        with self._lock:
            new = replace(self.settings, **_checked_settings(changes))
            self._write(SETTINGS_FILE, json.dumps(asdict(new), indent=1, ensure_ascii=False) + "\n")
            self._settings = new
            return new

    # Recents

    def recents(self) -> list[Recent]:
        """Newest first; files that no longer exist are marked missing, not dropped (11.4)."""
        with self._lock:
            entries = list(self._loaded_recents())
        return [replace(r, missing=_is_missing(r.uri)) for r in entries]

    def record_open(
        self,
        uri: str,
        display_name: str,
        kind: str,
        doc_key: str | None = None,
        thumb_key: str | None = None,
    ) -> Recent | None:
        """Put a file at the top of the recents; does nothing while history is off (11.5)."""
        if not self.settings.history_enabled:
            return None
        entry = Recent(uri, display_name, kind, _now(), thumb_key, doc_key)
        with self._lock:
            others = [r for r in self._loaded_recents() if r.uri != uri]
            kept, pushed_out = ([entry] + others)[:RECENTS_MAX], others[RECENTS_MAX - 1 :]
            self._save_recents(kept)
        if pushed_out:
            self._notify(_forgotten(pushed_out, (), everything=False, with_positions=False))
        return entry

    def set_thumb_key(self, uri: str, thumb_key: str | None) -> None:
        with self._lock:
            entries = self._loaded_recents()
            if any(r.uri == uri for r in entries):
                self._save_recents([replace(r, thumb_key=thumb_key) if r.uri == uri else r for r in entries])

    def remove(self, uri: str) -> None:
        """Forget one entry with its position; hooks purge its thumbnail (11.3)."""
        with self._lock:
            entries = self._loaded_recents()
            gone = [r for r in entries if r.uri == uri]
            if not gone:
                return
            self._save_recents([r for r in entries if r.uri != uri])
            keys = {k for k in map(_key_of, gone) if k}
            positions = self._loaded_positions()
            if keys & positions.keys():
                self._save_positions({k: p for k, p in positions.items() if k not in keys})
        self._notify(_forgotten(gone, tuple(keys), everything=False, with_positions=True))

    def clear_history(self) -> None:
        """Forget every recent and every position; hooks purge all thumbnails (11.3)."""
        with self._lock:
            entries = self._loaded_recents()
            keys = tuple(self._loaded_positions())
            self._save_recents([])
            self._save_positions({})
        self._notify(_forgotten(entries, keys, everything=True, with_positions=True))

    def add_forget_hook(self, hook: Callable[[Forgotten], None]) -> None:
        """Call ``hook`` after entries are removed, cleared or pushed off the list."""
        self._forget_hooks.append(hook)

    # Positions

    def position(self, key: str) -> Position | None:
        with self._lock:
            return self._loaded_positions().get(key)

    def save_position(self, key: str, page: int, offset_y: float = 0.0, zoom: float = 1.0) -> Position | None:
        """Remember where a document was left; keeps the 500 most recently saved (11.2, 11.5)."""
        if not self.settings.history_enabled:
            return None
        entry = Position(key, int(page), float(offset_y), float(zoom), _now())
        with self._lock:
            positions = dict(self._loaded_positions())
            positions.pop(key, None)
            positions[key] = entry
            newest = sorted(positions.values(), key=lambda p: p.updated_at, reverse=True)[:POSITIONS_MAX]
            self._save_positions({p.doc_key: p for p in newest})
        return entry

    # Files

    def _loaded_recents(self) -> list[Recent]:
        if self._recents is None:
            newest_first = sorted(self._read_lines(RECENTS_FILE, _recent_from), key=lambda r: -r.opened_at)
            unique: dict[str, Recent] = {}
            for r in newest_first:
                unique.setdefault(r.uri, r)
            self._recents = list(unique.values())[:RECENTS_MAX]
        return self._recents

    def _loaded_positions(self) -> dict[str, Position]:
        if self._positions is None:
            rows = self._read_lines(POSITIONS_FILE, _position_from)
            newest = sorted(rows, key=lambda p: p.updated_at)
            self._positions = {p.doc_key: p for p in newest}
        return self._positions

    def _save_recents(self, entries: list[Recent]) -> None:
        self._write(RECENTS_FILE, _lines(_stored(r) for r in entries))
        self._recents = list(entries)

    def _save_positions(self, positions: dict[str, Position]) -> None:
        self._write(POSITIONS_FILE, _lines(asdict(p) for p in positions.values()))
        self._positions = dict(positions)

    def _read_lines(self, name: str, parse: Callable[[dict], object]) -> list:
        text = self._read(name)
        if text is None:
            return []
        rows, bad = [], 0
        for line in text.splitlines():
            if not line.strip():
                continue
            try:
                rows.append(parse(json.loads(line)))
            except (ValueError, TypeError, KeyError, AttributeError):
                bad += 1
        if bad:
            log.warning("%s: skipped %d unreadable entries, kept %d", name, bad, len(rows))
            self._backup(name)
        return rows

    def _load_settings(self) -> Settings:
        text = self._read(SETTINGS_FILE)
        if text is None:
            return Settings()
        try:
            raw = json.loads(text)
            if not isinstance(raw, dict):
                raise ValueError("not an object")
        except ValueError:
            log.warning("%s is unreadable; using defaults", SETTINGS_FILE)
            self._backup(SETTINGS_FILE)
            return Settings()
        good = {}
        for name, value in raw.items():
            try:
                good.update(_checked_settings({name: value}))
            except ValueError:
                log.warning("%s: ignored %s=%r", SETTINGS_FILE, name, value)
        if len(good) != len(raw):
            self._backup(SETTINGS_FILE)
        return Settings(**good)

    def _read(self, name: str) -> str | None:
        try:
            return (self._folder / name).read_text(encoding="utf-8")
        except FileNotFoundError:
            return None
        except (OSError, UnicodeDecodeError) as e:
            log.warning("%s is unreadable (%s); starting empty", name, e)
            self._backup(name)
            return None

    def _backup(self, name: str) -> None:
        try:
            shutil.copyfile(self._folder / name, self._folder / f"{name}.bak")
        except OSError as e:
            log.warning("could not back up %s: %s", name, e)

    def _write(self, name: str, text: str) -> None:
        """Write a whole file atomically: a temporary file in the same folder, then rename."""
        self._folder.mkdir(parents=True, exist_ok=True)
        fd, tmp = tempfile.mkstemp(dir=self._folder, prefix=f".{name}.", suffix=".tmp")
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as f:
                f.write(text)
                f.flush()
                os.fsync(f.fileno())
            os.replace(tmp, self._folder / name)
        except BaseException:
            Path(tmp).unlink(missing_ok=True)
            raise

    def _notify(self, event: Forgotten) -> None:
        for hook in list(self._forget_hooks):
            try:
                hook(event)
            except Exception:  # noqa: BLE001 - a failing purge must not undo the removal
                log.exception("forget hook failed")


def _checked_settings(changes: dict) -> dict:
    known = {f.name for f in fields(Settings)}
    for name, value in changes.items():
        if name not in known:
            raise ValueError(f"unknown setting {name!r}")
        ok = {
            "language": value in LANGUAGES,
            "theme": value in THEMES,
            "night_mode": isinstance(value, bool),
            "history_enabled": isinstance(value, bool),
            "text_page_mb": type(value) is int and 1 <= value <= 1024,
        }[name]
        if not ok:
            raise ValueError(f"bad value for {name}: {value!r}")
    return changes


def _recent_from(raw: dict) -> Recent:
    entry = Recent(
        uri=raw["uri"],
        display_name=raw["display_name"],
        kind=raw["kind"],
        opened_at=float(raw["opened_at"]),
        thumb_key=raw.get("thumb_key"),
        doc_key=raw.get("doc_key"),
    )
    if not all(isinstance(v, str) and v for v in (entry.uri, entry.display_name, entry.kind)):
        raise ValueError("empty or non-text field")
    if not all(v is None or isinstance(v, str) for v in (entry.thumb_key, entry.doc_key)):
        raise ValueError("bad key")
    return entry


def _position_from(raw: dict) -> Position:
    if not isinstance(raw["doc_key"], str) or not raw["doc_key"] or isinstance(raw["page"], bool):
        raise ValueError("bad position")
    page = raw["page"]
    if not isinstance(page, int) or page < 0:
        raise ValueError("bad page")
    return Position(
        raw["doc_key"],
        page,
        float(raw.get("offset_y", 0.0)),
        float(raw.get("zoom", 1.0)),
        float(raw.get("updated_at", 0.0)),
    )


def _stored(r: Recent) -> dict:
    row = asdict(r)
    row.pop("missing")
    return row


def _lines(rows) -> str:
    return "".join(json.dumps(row, ensure_ascii=False) + "\n" for row in rows)


def _key_of(r: Recent) -> str | None:
    """The stored key, or one read from the file when an older entry has none."""
    if r.doc_key:
        return r.doc_key
    path = local_path(r.uri)
    return doc_key(path) if path else None


def _is_missing(uri: str) -> bool:
    path = local_path(uri)
    return path is not None and not path.exists()


def _forgotten(entries, keys, everything: bool, with_positions: bool) -> Forgotten:
    return Forgotten(
        uris=tuple(r.uri for r in entries),
        doc_keys=tuple(keys) if with_positions else (),
        thumb_keys=tuple(r.thumb_key for r in entries if r.thumb_key),
        everything=everything,
    )
