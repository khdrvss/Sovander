"""Fuzz the zip reader (Requirements 8, 10.4; NFR Robustness).

sovander.archive documents ArchiveError (Damaged, TooLarge, NeedsPassword, WrongPassword,
Unsupported) for anything it cannot read. A case lists every folder and reads up to 64 entries,
locked ones with a wrong password, and re-decodes the names in another code page (8.6).
"""

import pytest
from fuzzlib import FIXTURES, describe, iterations, run_campaign

from sovander.archive import Archive, ArchiveError

ZIPS = sorted(p for p in FIXTURES.glob("*.zip"))
# Office files, ODF and EPUB are zips too, with deep folders, many members and XML inside
CONTAINERS = sorted(p for p in FIXTURES.iterdir()
                    if p.suffix != ".zip" and p.is_file() and p.read_bytes()[:4] == b"PK\x03\x04")
FAMILIES = {"zip": ZIPS, "office-zip": CONTAINERS}
MAX_READS = 64


def open_everything(data: bytes, name: str) -> str:
    with Archive(data) as archive:
        folders = [""] + [e.path for e in archive.entries if e.is_dir]
        for folder in folders:
            archive.list(folder)
        refused = 0
        for entry in [e for e in archive.entries if not e.is_dir][:MAX_READS]:
            try:
                content = archive.read(entry, password="wrong" if entry.encrypted else None)
            except ArchiveError:
                refused += 1
                continue
            if len(content) != entry.size:
                raise AssertionError(f"{entry.path}: {len(content)} bytes read, index says {entry.size}")
        archive.set_name_encoding("cp866")
        return "some entries refused" if refused else "ok"


def test_seed_families_are_not_empty():
    assert len(ZIPS) >= 5 and len(CONTAINERS) >= 10


@pytest.mark.parametrize("family", sorted(FAMILIES))
def test_archive_reader_survives_malformed_zips(family):
    report = run_campaign("archive", family, FAMILIES[family], open_everything, allowed=(ArchiveError,),
                          timeout=20, cases=iterations())
    assert not report["failures"], describe(report)
