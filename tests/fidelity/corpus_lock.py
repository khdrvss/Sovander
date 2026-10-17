"""The reference corpus as tests/fidelity/corpus.lock records it (fetched by scripts/fetch-corpus.sh)."""

from __future__ import annotations

import hashlib
from dataclasses import dataclass
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
LOCK = Path(__file__).with_name("corpus.lock")
CORPUS = ROOT / "spike" / "corpus"


@dataclass(frozen=True)
class LockEntry:
    sha256: str
    size: int
    path: str
    url: str


def read_lock(lock: Path = LOCK) -> list[LockEntry]:
    entries = []
    for line in lock.read_text(encoding="utf-8").splitlines():
        if not line or line.startswith("#"):
            continue
        sha, size, path, url = line.split("\t")
        entries.append(LockEntry(sha, int(size), path, url))
    return entries


def present() -> bool:
    """Whether the corpus was fetched (any of its files is there)."""
    return CORPUS.is_dir() and any(CORPUS.iterdir())


def sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with open(path, "rb") as fh:
        for block in iter(lambda: fh.read(1 << 20), b""):
            digest.update(block)
    return digest.hexdigest()
