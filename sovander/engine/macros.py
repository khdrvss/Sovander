"""Find macros in a document without running anything (Requirements 10.1, 10.2).

Pure Python on the file's container: zip member names for OOXML and ODF, and the directory of an OLE
compound file for legacy Office formats. Damaged or hostile input never raises; it reports no macros.
"""

from __future__ import annotations

import io
import struct
import zipfile
from pathlib import Path

CFB_MAGIC = b"\xd0\xcf\x11\xe0\xa1\xb1\x1a\xe1"
ZIP_MAGIC = b"PK\x03\x04"
# Storages that hold VBA in .doc (Macros), .xls (_VBA_PROJECT_CUR) and the VBA project itself
OLE_MACRO_NAMES = {"macros", "_vba_project_cur", "_vba_project", "vba"}
_FREE, _END_OF_CHAIN = 0xFFFFFFFF, 0xFFFFFFFE
_MAX_DIR_SECTORS = 1 << 16  # a directory this long is hostile, not a document


def has_macros(source: Path | bytes) -> bool:
    """True when the document carries macro code (it is never executed: the engine runs none)."""
    return bool(macro_parts(source))


def macro_parts(source: Path | bytes) -> list[str]:
    """Names of the parts that hold macros, e.g. ["word/vbaProject.bin"]; empty when there are none."""
    try:
        data = source.read_bytes() if isinstance(source, Path) else bytes(source)
    except OSError:
        return []
    if data.startswith(ZIP_MAGIC):
        return _zip_parts(data)
    if data.startswith(CFB_MAGIC):
        return _ole_parts(data)
    return []


def _zip_parts(data: bytes) -> list[str]:
    try:
        names = zipfile.ZipFile(io.BytesIO(data)).namelist()
    except (zipfile.BadZipFile, ValueError, OSError):
        return []
    found = []
    for name in names:
        lower = name.lower()
        if lower.endswith("/"):
            continue
        if (lower.endswith("vbaproject.bin")  # OOXML VBA: word/, xl/, ppt/
                or lower.startswith("xl/macrosheets/")  # Excel 4.0 macro sheets
                # ODF Basic modules (library indexes alone are not code); Python/JavaScript/BeanShell
                or (lower.startswith("basic/") and not lower.endswith(("-lc.xml", "-lb.xml")))
                or lower.startswith("scripts/")):
            found.append(name)
    return found


def _ole_parts(data: bytes) -> list[str]:
    try:
        return [name for name in ole_names(data) if name.lower() in OLE_MACRO_NAMES]
    except (struct.error, ValueError, IndexError):
        return []


def ole_names(data: bytes) -> list[str]:
    """Names of every storage and stream in an OLE compound file (MS-CFB), read from its directory.

    The directory is walked as a flat list of entries rather than as a tree: a damaged tree cannot hide
    an entry or send the reader in circles.
    """
    if len(data) < 512 or not data.startswith(CFB_MAGIC):
        raise ValueError("not a compound file")
    shift = struct.unpack_from("<H", data, 0x1E)[0]
    if shift not in (9, 12):
        raise ValueError("bad sector size")
    size = 1 << shift
    num_fat, first_dir = struct.unpack_from("<II", data, 0x2C)
    first_difat, num_difat = struct.unpack_from("<II", data, 0x44)

    def sector(index: int) -> bytes:
        start = (index + 1) * size  # the header occupies the first sector's slot
        if start + size > len(data):
            raise ValueError("sector outside the file")
        return data[start: start + size]

    fat_sectors = [s for s in struct.unpack_from("<109I", data, 0x4C) if s not in (_FREE, _END_OF_CHAIN)]
    seen = set()
    next_difat = first_difat
    for _ in range(min(num_difat, _MAX_DIR_SECTORS)):
        if next_difat in (_FREE, _END_OF_CHAIN) or next_difat in seen:
            break
        seen.add(next_difat)
        block = struct.unpack(f"<{size // 4}I", sector(next_difat))
        fat_sectors += [s for s in block[:-1] if s not in (_FREE, _END_OF_CHAIN)]
        next_difat = block[-1]
    fat: list[int] = []
    for index in fat_sectors[:num_fat]:
        fat += struct.unpack(f"<{size // 4}I", sector(index))

    names = []
    current, visited = first_dir, set()
    while current < len(fat) and current not in visited and len(visited) < _MAX_DIR_SECTORS:
        visited.add(current)
        block = sector(current)
        for offset in range(0, size, 128):
            entry = block[offset: offset + 128]
            name_len = struct.unpack_from("<H", entry, 64)[0]
            kind = entry[66]
            if kind in (1, 2, 5) and 2 <= name_len <= 64:  # storage, stream, root
                names.append(entry[: name_len - 2].decode("utf-16-le", "replace"))
        current = fat[current]
    return names
