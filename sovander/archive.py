"""Zip archives read in memory: a folder listing, and any entry as bytes (Requirements 8, 10.4).

Nothing is written to disk (8.3). Ported in behaviour from Gander's ZipReader and ZipEncryption
(MIT). The index is parsed here, not by `zipfile`: zipnames needs the raw name bytes, `zipfile`
refuses a whole archive over one bad UTF-8 name and inflates bzip2/LZMA with no output bound.
"""

import bz2
import hashlib
import hmac
import lzma
import os
import struct
import sys
import zlib
from array import array
from collections import defaultdict
from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime

from sovander import zipnames

MAX_ENTRIES = 200_000
MAX_INDEX_BYTES = 32 * 1024 * 1024  # an index claiming more is refused unread
MAX_ENTRY_BYTES = 2 * 1024**3
# Past the floor, an entry that expands more than this many times its packed size is refused as
# a likely bomb before any of it is inflated. Real documents and logs stay far below it.
RATIO_LIMIT = 100
RATIO_FLOOR = 256 * 1024 * 1024

STORED, DEFLATED, DEFLATE64, BZIP2, LZMA, AES = 0, 8, 9, 12, 14, 99
_READABLE = {STORED, DEFLATED, BZIP2, LZMA}

_CENTRAL = struct.Struct("<4s6H3I5H2I")  # 46 bytes
_LOCAL = struct.Struct("<4s5H3I2H")  # 30 bytes

# Code pages a ZipCrypto password may have been typed in: WinZip AES and anything from macOS or
# Linux use UTF-8, a Windows tool the machine's own code page (cp866 on a Russian one).
_PASSWORD_ENCODINGS = ("utf-8", "cp866", "cp1251", "cp437", "cp850", "cp1252", "cp1250")


class ArchiveError(Exception):
    """Why an archive or one of its entries cannot be read."""


class Damaged(ArchiveError):
    """Not a zip, cut short, or an entry that fails its checks."""


class TooLarge(ArchiveError):
    """Over the caps of Requirement 10.4: too many entries, or an entry too large to hold."""


class NeedsPassword(ArchiveError):
    """An encrypted entry asked for without a password."""


class WrongPassword(ArchiveError):
    """A password that opens the entry in none of the ways it could have been typed."""


class Unsupported(ArchiveError):
    """A method or encryption the Viewer does not read: Deflate64, PKWARE strong encryption."""


@dataclass(frozen=True)
class Entry:
    """One file or folder. `index` is its place in the zip's index, -1 for a folder only implied by names."""

    path: str
    is_dir: bool
    size: int = 0
    compressed_size: int = 0
    modified: datetime | None = None
    encrypted: bool = False
    supported: bool = True
    index: int = -1

    @property
    def name(self) -> str:
        return self.path.rpartition("/")[2]

    @property
    def parent(self) -> str:
        return self.path.rpartition("/")[0]

    @property
    def hidden(self) -> bool:
        """A dotfile or macOS's __MACOSX folder, which a listing may leave out."""
        return self.name.startswith(".") or self.path.partition("/")[0] == "__MACOSX"


@dataclass(frozen=True)
class _Record:
    """What the index says about one entry, before its name is decoded."""

    raw_name: bytes
    flags: int
    method: int  # inside the encryption, for AES
    crc: int
    compressed: int
    size: int
    offset: int
    dos: bool  # written on a host that separates folders with a backslash
    folder: bool
    date: int
    time: int
    lock: str  # "none", "zipcrypto", "aes" or "unsupported"
    aes_strength: int
    aes_version: int
    unicode_path: bytes | None


class Archive:
    """A zip opened from a path or from bytes (a zip inside a zip arrives as bytes)."""

    def __init__(self, source: str | os.PathLike | bytes, name_encoding: str | None = None,
                 lang: str | None = None):
        self._fd: int | None = None
        if isinstance(source, (bytes, bytearray, memoryview)):
            self._bytes = bytes(source)
            self._length = len(self._bytes)
        else:
            self._fd = os.open(source, os.O_RDONLY | os.O_CLOEXEC)
            self._length = os.fstat(self._fd).st_size
        try:
            self._records = _read_index(self._read, self._length)
        except BaseException:
            self.close()
            raise
        self._lang = lang
        self.set_name_encoding(name_encoding)

    def close(self) -> None:
        if self._fd is not None:
            os.close(self._fd)
            self._fd = None

    def __enter__(self) -> "Archive":
        return self

    def __exit__(self, *_exc: object) -> None:
        self.close()

    def set_name_encoding(self, encoding: str | None) -> None:
        """Read the names again in `encoding` (8.6), or guess again with None."""
        rs = self._records
        raw, flags, paths = [r.raw_name for r in rs], [r.flags for r in rs], [r.unicode_path for r in rs]
        self.name_encoding, names = zipnames.decode_names(raw, flags, encoding, self._lang, paths)
        self.entries: list[Entry] = []
        for index, (rec, name) in enumerate(zip(rs, names, strict=True)):
            path = normalise(name, rec.dos)
            if not path:
                continue
            # Read off the decoded name: the last byte of 表 in Shift_JIS is a backslash
            is_dir = rec.folder or name.endswith("/") or (rec.dos and name.endswith("\\"))
            self.entries.append(Entry(
                path, is_dir, rec.size, rec.compressed, _dos_time(rec.date, rec.time),
                rec.lock != "none", rec.lock != "unsupported" and rec.method in _READABLE, index,
            ))
        self._children = _tree(self.entries)

    def list(self, folder: str = "") -> list[Entry]:
        """What is directly inside `folder` ("" is the top): folders first, then by name."""
        return list(self._children.get(folder.strip("/"), ()))

    def read(self, entry: Entry, password: str | bytes | None = None) -> bytes:
        """The entry's content, in memory. Raises an ArchiveError subclass when it cannot be read."""
        if entry.is_dir or not 0 <= entry.index < len(self._records):
            raise ValueError(f"{entry.path!r} is not a file of this archive")
        rec = self._records[entry.index]
        if rec.lock == "unsupported" or rec.method not in _READABLE:
            raise Unsupported(f"{entry.path}: method {rec.method} or its encryption is not supported")
        if rec.size > MAX_ENTRY_BYTES or (rec.size > RATIO_FLOOR and rec.size > RATIO_LIMIT * rec.compressed):
            raise TooLarge(f"{entry.path}: {rec.size} bytes expanded is too large to open safely")
        raw = self._read(self._data_start(rec), rec.compressed)
        if rec.lock == "none":
            return _unpack(raw, rec.method, rec.size, rec.crc)
        if password is None:
            raise NeedsPassword(entry.path)
        candidates = _password_bytes(password)
        return _zipcrypto(raw, rec, candidates) if rec.lock == "zipcrypto" else _aes(raw, rec, candidates)

    def _read(self, position: int, count: int) -> bytes:
        if position < 0 or count < 0 or position + count > self._length:
            raise Damaged("the zip ends early")
        if self._fd is None:
            return self._bytes[position:position + count]
        chunks, done = [], 0
        while done < count:
            chunk = os.pread(self._fd, count - done, position + done)
            if not chunk:
                raise Damaged("the zip ends early")
            chunks.append(chunk)
            done += len(chunk)
        return b"".join(chunks)

    def _data_start(self, rec: _Record) -> int:
        header = _LOCAL.unpack(self._read(rec.offset, _LOCAL.size))
        if header[0] != b"PK\x03\x04":
            raise Damaged(f"no entry at {rec.offset}")
        return rec.offset + _LOCAL.size + header[9] + header[10]


def member_names(read: Callable[[int, int], bytes], length: int, limit: int = 4096) -> list[bytes]:
    """The raw names of the first `limit` entries, for the router to tell OOXML/ODF/EPUB from a zip."""
    start, size, _count, _shift = _central_directory(read, length)
    names: list[bytes] = []
    try:
        for _fields, name, _extra in _central_records(read(start, min(size, 1 << 20))):
            names.append(name)
            if len(names) >= limit:
                break
    except Damaged:
        pass  # an index cut short by the read limit still names its first members
    return names


def normalise(name: str, dos: bool) -> str:
    """A name safe to show and split on "/": no empty, "." or ".." segment, DOS backslashes as
    separators, control and bidi marks ("photo\\u202egpj.apk") as U+FFFD. Never used as a path."""
    if dos:
        name = name.replace("\\", "/")
    segments = [s for s in name.split("/") if s not in ("", ".", "..")]
    return "/".join("".join("\ufffd" if _unsafe(c) else c for c in s) for s in segments)


def _unsafe(c: str) -> bool:
    return c < " " or "\x7f" <= c <= "\x9f" or "\u202a" <= c <= "\u202e" or "\u2066" <= c <= "\u2069"


def _tree(entries: list[Entry]) -> dict[str, list[Entry]]:
    folders = {e.path: e for e in reversed(entries) if e.is_dir}  # the first of a repeated folder wins
    for e in entries:
        parent = e.parent
        while parent and parent not in folders:
            folders[parent] = Entry(parent, True)
            parent = folders[parent].parent
    children: dict[str, list[Entry]] = defaultdict(list)
    for e in [*folders.values(), *(e for e in entries if not e.is_dir)]:
        children[e.parent].append(e)
    for items in children.values():
        items.sort(key=lambda e: (not e.is_dir, e.name.casefold(), e.name))
    return dict(children)


def _central_directory(read: Callable[[int, int], bytes], length: int) -> tuple[int, int, int, int]:
    """Index start, size, entry count, and the shift of a zip with something in front of it.

    A comment can hold the end record's signature, so the search runs backwards for a record
    whose comment ends the file exactly, else the last one that fits (junk after the zip)."""
    tail_start = max(0, length - 22 - 0xFFFF)
    tail = read(tail_start, length - tail_start)
    at, fallback = tail.rfind(b"PK\x05\x06", 0, len(tail) - 18), -1
    while at >= 0 and (fits := at + 22 + int.from_bytes(tail[at + 20:at + 22], "little")) != len(tail):
        fallback = at if fallback < 0 and fits < len(tail) else fallback
        at = tail.rfind(b"PK\x05\x06", 0, at)
    at = at if at >= 0 else fallback
    if at < 0:
        raise Damaged("not a zip")
    end = tail_start + at
    disk, _, _, count, size, offset = struct.unpack_from("<4H2I", tail, at + 4)
    index_end = end
    # ZIP64, looked for whether or not the end record asks: Info-ZIP writes the records for a
    # piped file and leaves the end record's fields as they are. Right before the locator first,
    # since the offset the locator gives is wrong for a zip with something in front of it.
    record_at = None
    if end >= 20 and read(end - 20, 4) == b"PK\x06\x07":
        candidates = (end - 76, int.from_bytes(read(end - 12, 8), "little"))
        found = (c for c in candidates if 0 <= c <= length - 56 and read(c, 4) == b"PK\x06\x06")
        record_at = next(found, None)
    if record_at is not None:
        disk, _, _, count, size, offset = struct.unpack("<2I4Q", read(record_at + 16, 40))
        index_end = record_at
    elif count == 0xFFFF or size == 0xFFFFFFFF or offset == 0xFFFFFFFF:
        raise Damaged("ZIP64 record missing")
    if disk not in (0, 0xFFFF):
        raise Damaged("split across disks")
    shift = index_end - (offset + size)
    if shift < 0 or count * _CENTRAL.size > size:
        raise Damaged("damaged end record")
    return offset + shift, size, count, shift


def _central_records(data: bytes):
    """Each index record in `data`: its fixed fields, raw name and extra field."""
    p = 0
    while p < len(data):
        if p + _CENTRAL.size > len(data) or data[p:p + 4] != b"PK\x01\x02":
            raise Damaged("damaged index")
        fields = _CENTRAL.unpack_from(data, p)
        name_at = p + _CENTRAL.size
        extra_at = name_at + fields[10]
        p = extra_at + fields[11] + fields[12]
        if p > len(data):
            raise Damaged("damaged index")
        yield fields, data[name_at:extra_at], data[extra_at:extra_at + fields[11]]


def _read_index(read: Callable[[int, int], bytes], length: int) -> list[_Record]:
    start, size, count, shift = _central_directory(read, length)
    if size > MAX_INDEX_BYTES or count > MAX_ENTRIES:
        raise TooLarge("too many entries to list")
    records = []
    # Walked by size, not count: a 16-bit count wraps past 65,535 when no ZIP64 record is written
    for fields, name, extra in _central_records(read(start, size)):
        if len(records) >= MAX_ENTRIES:
            raise TooLarge("too many entries to list")
        records.append(_record(fields, name, extra, shift))
    return records


def _record(fields: tuple, name: bytes, extra: bytes, shift: int) -> _Record:
    made_by, flags, method, time, date = fields[1], fields[3], fields[4], fields[5], fields[6]
    crc, compressed, size, external, offset = fields[7], fields[8], fields[9], fields[15], fields[16]
    unicode_path, aes_version, aes_strength, aes_method = None, 0, 0, -1
    p = 0
    while p + 4 <= len(extra):
        tag, length = struct.unpack_from("<2H", extra, p)
        data = extra[p + 4:p + 4 + length]
        if len(data) < length:
            break
        if tag == 0x0001:  # ZIP64: 64-bit values for exactly the fields left at all ones, in order
            values = iter(struct.unpack_from(f"<{length // 8}Q", data))
            size = next(values, size) if size == 0xFFFFFFFF else size
            compressed = next(values, compressed) if compressed == 0xFFFFFFFF else compressed
            offset = next(values, offset) if offset == 0xFFFFFFFF else offset
        elif tag == 0x7075:
            unicode_path = data
        elif tag == 0x9901 and length >= 7:
            aes_version, _, aes_strength, aes_method = struct.unpack_from("<H2sBH", data)
        p += 4 + length
    host = made_by >> 8
    dos = host in (0, 6, 10, 14)  # FAT, HPFS, NTFS, VFAT
    folder = (host == 3 and (external >> 16) & 0xF000 == 0x4000) or (dos and external & 0x10 and size == 0)
    if not flags & 1:
        lock = "unsupported" if method == AES else "none"
    elif flags & 0x40:
        lock = "unsupported"  # PKWARE strong encryption
    elif method != AES:
        lock = "zipcrypto"
    else:
        lock = "aes" if aes_strength in (1, 2, 3) else "unsupported"
    if method == AES and aes_method >= 0:
        method = aes_method
    return _Record(
        name, flags, method, crc, compressed, size, offset + shift, dos, bool(folder),
        date, time, lock, aes_strength, aes_version, unicode_path,
    )


def _dos_time(date: int, time: int) -> datetime | None:
    try:
        return datetime(1980 + (date >> 9), (date >> 5) & 0xF, date & 0x1F, time >> 11, (time >> 5) & 0x3F,
                        (time & 0x1F) * 2)
    except ValueError:
        return None  # the zero date some writers leave, or one that cannot be a date


def _unpack(data: bytes, method: int, size: int, crc: int | None) -> bytes:
    """Inflate with the output bounded one byte past the declared size, then hold it to the index."""
    try:
        if method == STORED:
            out = data
        elif method == DEFLATED:
            out = zlib.decompressobj(-15).decompress(data, size + 1)
        elif method == BZIP2:
            out = bz2.BZ2Decompressor().decompress(data, size + 1)
        else:
            decompressor, stream = _lzma(data)
            out = decompressor.decompress(stream, size + 1)
    except (zlib.error, lzma.LZMAError, OSError, EOFError, ValueError) as e:
        raise Damaged(f"entry is damaged: {e}") from e
    if len(out) != size:
        raise Damaged("entry size differs from its index")
    if crc is not None and zlib.crc32(out) != crc:
        raise Damaged("entry fails its checksum")
    return out


def _lzma(data: bytes) -> tuple[lzma.LZMADecompressor, bytes]:
    """Zip's LZMA: a 4-byte header and 5 bytes of LZMA1 properties before a raw stream."""
    props = data[4:9]
    if data[2:4] != b"\x05\x00" or len(props) != 5 or props[0] >= 9 * 5 * 5:
        raise ValueError("bad LZMA properties")
    lc, lp, pb = props[0] % 9, props[0] // 9 % 5, props[0] // 45
    dict_size = int.from_bytes(props[1:5], "little")
    lzma_filter = {"id": lzma.FILTER_LZMA1, "dict_size": dict_size, "lc": lc, "lp": lp, "pb": pb}
    return lzma.LZMADecompressor(lzma.FORMAT_RAW, filters=[lzma_filter]), data[9:]


def _password_bytes(password: str | bytes) -> list[bytes]:
    """Every way `password` could have been typed as bytes, each once, the likeliest first."""
    if isinstance(password, bytes):
        return [password]
    out: list[bytes] = []
    for encoding in dict.fromkeys(_PASSWORD_ENCODINGS + zipnames.CHOICES):
        try:
            candidate = password.encode(encoding)
        except UnicodeEncodeError:
            continue
        if candidate and candidate not in out:
            out.append(candidate)
    return out


def _crc_byte(c: int) -> int:
    for _ in range(8):
        c = (c >> 1) ^ 0xEDB88320 if c & 1 else c >> 1
    return c


_CRC_TABLE = [_crc_byte(n) for n in range(256)]  # CRC-32's, which ZipCrypto stirs its keys with


def _zipcrypto_keys(password: bytes) -> list[int]:
    k0, k1, k2 = 0x12345678, 0x23456789, 0x34567890
    for c in password:
        k0 = _CRC_TABLE[(k0 ^ c) & 0xFF] ^ (k0 >> 8)
        k1 = ((k1 + (k0 & 0xFF)) * 134775813 + 1) & 0xFFFFFFFF
        k2 = _CRC_TABLE[(k2 ^ (k1 >> 24)) & 0xFF] ^ (k2 >> 8)
    return [k0, k1, k2]


def _zipcrypto(raw: bytes, rec: _Record, passwords: list[bytes]) -> bytes:
    """PKWARE's traditional encryption: 12 header bytes, then the packed entry."""
    if len(raw) < 12:
        raise Damaged("damaged encryption header")
    # The header's last byte is the checksum's top byte, or the time's when the sizes trail the data
    check = (rec.time >> 8) & 0xFF if rec.flags & 8 else rec.crc >> 24
    for password in passwords:
        keys = _zipcrypto_keys(password)
        if _decrypt(raw[:12], keys)[11] != check:
            continue
        try:
            return _unpack(_decrypt(raw[12:], keys), rec.method, rec.size, rec.crc)
        except Damaged:
            continue  # one wrong password in 256 passes the check byte; the checksum catches it
    raise WrongPassword("wrong password")


def _decrypt(data: bytes, keys: list[int]) -> bytes:
    k0, k1, k2 = keys
    table = _CRC_TABLE
    out = bytearray(len(data))
    for i, c in enumerate(data):
        t = (k2 | 2) & 0xFFFF
        c ^= ((t * (t ^ 1)) >> 8) & 0xFF
        out[i] = c
        k0 = table[(k0 ^ c) & 0xFF] ^ (k0 >> 8)
        k1 = ((k1 + (k0 & 0xFF)) * 134775813 + 1) & 0xFFFFFFFF
        k2 = table[(k2 ^ (k1 >> 24)) & 0xFF] ^ (k2 >> 8)
    keys[:] = k0, k1, k2
    return bytes(out)


def _aes(raw: bytes, rec: _Record, passwords: list[bytes]) -> bytes:
    """WinZip AES: salt and a 2-byte password verifier before the data, a 10-byte HMAC after."""
    key_size = 8 + 8 * rec.aes_strength
    salt_size = key_size // 2
    if len(raw) < salt_size + 2 + 10:
        raise Damaged("damaged encryption header")
    salt, verifier = raw[:salt_size], raw[salt_size:salt_size + 2]
    body, code = raw[salt_size + 2:-10], raw[-10:]
    for password in passwords:
        derived = hashlib.pbkdf2_hmac("sha1", password, salt, 1000, 2 * key_size + 2)
        if derived[-2:] != verifier:
            continue
        mac = hmac.new(derived[key_size:2 * key_size], body, hashlib.sha1).digest()[:10]
        if not hmac.compare_digest(mac, code):
            raise Damaged("entry fails its authentication code")
        # AE-2 leaves the checksum at zero: the authentication code does its job
        crc = None if rec.aes_version == 2 else rec.crc
        return _unpack(_aes_ctr(derived[:key_size], body), rec.method, rec.size, crc)
    raise WrongPassword("wrong password")


def _aes_ctr(key: bytes, data: bytes, chunk: int = 1 << 20) -> bytes:
    """AES-CTR with WinZip's counter, little-endian from 1. Standard CTR counts big-endian, so the
    counter blocks are encrypted here (ECB over counters only, never data) and XORed in."""
    from cryptography.hazmat.primitives.ciphers import Cipher, algorithms, modes

    encrypt = Cipher(algorithms.AES(key), modes.ECB()).encryptor().update
    out = bytearray()
    for start in range(0, len(data), chunk):
        piece = data[start:start + chunk]
        blocks = -(-len(piece) // 16)
        first = start // 16 + 1
        counters = array("Q", bytes(16 * blocks))
        counters[0::2] = array("Q", range(first, first + blocks))
        if sys.byteorder == "big":
            counters.byteswap()
        stream = encrypt(counters.tobytes())[:len(piece)]
        mixed = int.from_bytes(piece, "little") ^ int.from_bytes(stream, "little")
        out += mixed.to_bytes(len(piece), "little")
    return bytes(out)
