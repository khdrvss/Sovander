"""Which viewer a file goes to, decided by its content (Requirements 2, 2.11, 7).

The extension only settles what the content leaves open: which text format a text file is, and
which Office family an encrypted or unreadable OLE container belongs to. A .pdf that is text
opens as text, a .doc that is RTF opens as RTF, a .zip that is a .docx opens as Word.
"""

import enum
import os
import re
import struct
from collections.abc import Callable

from sovander.archive import ArchiveError, member_names

HEAD_BYTES = 64 * 1024
TEXT_SNIFF_BYTES = 8000  # what git reads to tell text from binary


class Kind(enum.Enum):
    PDF = "pdf"
    WORD = "word"  # .docx, .docm, .dotx
    WORD_LEGACY = "word-legacy"  # Word 97-2003 .doc
    ODT = "odt"
    RTF = "rtf"
    SHEET = "sheet"  # .xlsx .xlsm .xlsb .xltx .xls .ods .csv
    SLIDES = "slides"  # .pptx .ppsx .pptm .potx .odp .ppt
    DRAWING = "drawing"  # .odg
    EPUB = "epub"
    MARKDOWN = "markdown"
    IMAGE = "image"
    TEXT = "text"
    ZIP = "zip"
    UNKNOWN_BINARY = "unknown-binary"


# What text a known extension names, for the "shown as text" note of Requirement 7.1
TEXT_EXTENSIONS = frozenset({
    "txt", "text", "log", "md", "markdown", "csv", "tsv", "json", "xml", "yaml", "yml", "toml", "ini",
    "cfg", "conf", "properties", "html", "htm", "css", "js", "ts", "py", "kt", "java", "c", "cpp", "h",
    "hpp", "rs", "go", "rb", "php", "sql", "swift", "dart", "sh", "bash", "zsh", "gradle", "tex", "r",
    "srt", "vtt", "m3u", "nfo", "v", "vh", "sv", "svh", "vhd", "vhdl",
})
_MARKDOWN_EXTENSIONS = {"md", "markdown", "mdown", "mkd"}
_SHEET_TEXT_EXTENSIONS = {"csv", "tsv"}
# The Office family an extension names, for OLE containers whose streams do not say
_OFFICE_EXTENSIONS = {
    **dict.fromkeys(("docx", "docm", "dotx", "dotm"), Kind.WORD),
    **dict.fromkeys(("doc", "dot"), Kind.WORD_LEGACY),
    **dict.fromkeys(("xls", "xlsx", "xlsm", "xlsb", "xltx", "xlt"), Kind.SHEET),
    **dict.fromkeys(("ppt", "pptx", "ppsx", "pptm", "potx", "pps", "pot"), Kind.SLIDES),
}
_ODF_KINDS = {
    "text": Kind.ODT, "spreadsheet": Kind.SHEET, "presentation": Kind.SLIDES, "graphics": Kind.DRAWING,
}
_OOXML_FOLDERS = (("word", Kind.WORD), ("xl", Kind.SHEET), ("ppt", Kind.SLIDES))
_OLE_STREAMS = (
    ("WordDocument", Kind.WORD_LEGACY), ("Workbook", Kind.SHEET), ("Book", Kind.SHEET),
    ("PowerPoint Document", Kind.SLIDES),
)
_IMAGE_BRANDS = {b"heic", b"heix", b"heim", b"heis", b"hevc", b"hevx", b"mif1", b"msf1", b"avif", b"avis"}
_SVG = re.compile(
    rb"^(?:\xef\xbb\xbf)?\s*(?:<\?[^>]*\?>\s*|<!--.*?-->\s*|<!DOCTYPE[^>\[]*(?:\[.*?\])?\s*>\s*)*<svg[\s>/]",
    re.DOTALL | re.IGNORECASE,
)
_FLAT_ODF = re.compile(rb'office:mimetype="application/vnd\.oasis\.opendocument\.(\w+)')

Reader = Callable[[int, int], bytes]


def detect_path(path: str | os.PathLike) -> Kind:
    """Route a file on disk: its first 64 KB, plus the zip index or OLE directory where needed."""
    with open(path, "rb") as f:
        size = os.fstat(f.fileno()).st_size
        head = f.read(HEAD_BYTES)
        fd = f.fileno()

        def read(offset: int, count: int) -> bytes:
            return os.pread(fd, count, offset)

        return detect(head, os.fspath(path), read=read, size=size)


def detect_bytes(data: bytes, name: str = "") -> Kind:
    """Route a file held in memory, such as an archive entry (Requirement 8.2)."""
    return detect(data[:HEAD_BYTES], name, read=_slicer(data), size=len(data))


def detect(head: bytes, name: str = "", *, read: Reader | None = None, size: int | None = None) -> Kind:
    """Route a file from its first bytes and name. Content wins over the extension (2.11).

    `read(offset, count)` and `size` let it look past the head, at a zip's index at the end
    and at an OLE directory; without them only the head is consulted.
    """
    read = read or _slicer(head)
    total = len(head) if size is None else size
    ext = _extension(name)
    if head.startswith(b"%PDF-") or (ext == "pdf" and b"%PDF-" in head[:1024]):
        return Kind.PDF
    if head.startswith((b"PK\x03\x04", b"PK\x05\x06", b"PK\x07\x08")):
        return _zip_kind(head, read, total, ext)
    if head.startswith(b"\xd0\xcf\x11\xe0\xa1\xb1\x1a\xe1"):
        return _ole_kind(read, ext)
    if head.startswith(b"{\\rtf"):
        return Kind.RTF
    if _is_binary_image(head):
        return Kind.IMAGE
    if not reads_as_text(head):
        return Kind.UNKNOWN_BINARY
    if _SVG.match(head):
        return Kind.IMAGE
    flat = _FLAT_ODF.search(head[:4096])  # .fodt, .fods, .fodp, .fodg: OpenDocument as one XML file
    family = flat.group(1).decode() if flat else ""
    if family in _ODF_KINDS:
        return _ODF_KINDS[family]
    if ext in _MARKDOWN_EXTENSIONS:
        return Kind.MARKDOWN
    if ext in _SHEET_TEXT_EXTENSIONS:
        return Kind.SHEET
    return Kind.TEXT


def reads_as_text(head: bytes) -> bool:
    """Whether bytes of no known format read as text (Requirement 7.1). Ported from Gander.

    Text has no zero byte and nearly everything else has one early; UTF-16 is half zeros and
    still text, so a byte order mark counts.
    """
    if head.startswith((b"\xff\xfe", b"\xfe\xff")):
        return True
    return b"\x00" not in head[:TEXT_SNIFF_BYTES]


def is_known_text_name(name: str) -> bool:
    """Whether the name says text, so a text view needs no "shown as text" note (7.1)."""
    return _extension(name) in TEXT_EXTENSIONS


def _slicer(data: bytes) -> Reader:
    return lambda offset, count: data[offset:offset + count]


def _extension(name: str) -> str:
    base = os.path.basename(name)
    return base.rpartition(".")[2].lower() if "." in base.lstrip(".") else ""


def _zip_kind(head: bytes, read: Reader, size: int, ext: str) -> Kind:
    names, mimetype = _local_members(head)
    try:
        names += [n.decode("utf-8", "replace") for n in member_names(read, size)]
    except ArchiveError:
        pass  # the head was all there was to go on, or the index is damaged: the local headers decide
    if mimetype is not None:
        if mimetype == "application/epub+zip":
            return Kind.EPUB
        family = mimetype.partition("application/vnd.oasis.opendocument.")[2].partition("-")[0]
        if family in _ODF_KINDS:
            return _ODF_KINDS[family]
    members = set(names)
    if "[Content_Types].xml" in members:
        tops = {n.partition("/")[0] for n in members}
        found = [kind for folder, kind in _OOXML_FOLDERS if folder in tops]
        if found:
            by_name = _OFFICE_EXTENSIONS.get(ext)
            return by_name if by_name in found else found[0]
    if "META-INF/container.xml" in members:
        return Kind.EPUB
    if "content.xml" in members and "META-INF/manifest.xml" in members:
        # ODF without a readable mimetype member: the extension names the family
        family = {"odt": "text", "ott": "text", "ods": "spreadsheet", "ots": "spreadsheet",
                  "odp": "presentation", "otp": "presentation", "odg": "graphics", "otg": "graphics"}.get(ext)
        if family:
            return _ODF_KINDS[family]
    return Kind.ZIP


def _local_members(head: bytes) -> tuple[list[str], str | None]:
    """Names from the local headers in the head, and the `mimetype` member's content if stored there."""
    names: list[str] = []
    mimetype = None
    p = 0
    while head[p:p + 4] == b"PK\x03\x04" and p + 30 <= len(head):
        flags, method = struct.unpack_from("<2H", head, p + 6)
        packed, _, name_len, extra_len = struct.unpack_from("<2I2H", head, p + 18)
        name = head[p + 30:p + 30 + name_len].decode("utf-8", "replace")
        data_at = p + 30 + name_len + extra_len
        names.append(name)
        if name == "mimetype" and method == 0 and mimetype is None:
            mimetype = head[data_at:data_at + packed].decode("ascii", "replace").strip()
        if flags & 8 and packed == 0:
            break  # sizes trail the data: no way to find the next header from here
        p = data_at + packed
    return names, mimetype


def _ole_kind(read: Reader, ext: str) -> Kind:
    """Word, Excel or PowerPoint 97-2003 by the streams an OLE compound file holds.

    Anything else in one (an encrypted OOXML package, an Outlook message) goes by extension.
    """
    streams = _ole_streams(read)
    for stream, kind in _OLE_STREAMS:
        if stream in streams:
            return kind
    return _OFFICE_EXTENSIONS.get(ext, Kind.UNKNOWN_BINARY)


def _ole_streams(read: Reader, max_sectors: int = 256) -> set[str]:
    """Names of the streams directly in a compound file's root storage: a minimal read of [MS-CFB].

    Only the root's own children count: a workbook with a Word object embedded in it holds a
    WordDocument stream too, one storage down.
    """
    header = read(0, 512)
    if len(header) < 512 or struct.unpack_from("<H", header, 0x1E)[0] not in (9, 12):
        return set()
    sector_size = 1 << struct.unpack_from("<H", header, 0x1E)[0]
    fat_count, first_directory = struct.unpack_from("<2I", header, 0x2C)
    fat_sectors = struct.unpack_from("<109I", header, 0x4C)[:min(fat_count, 109)]
    per_fat = sector_size // 4
    fats: dict[int, bytes] = {}

    def sector(n: int) -> bytes:
        return read((n + 1) * sector_size, sector_size)

    entries: list[tuple[str, int, int, int, int]] = []  # name, type, left, right, child
    n, seen = first_directory, set()
    while n < 0xFFFFFFFA and n not in seen and len(seen) < max_sectors:
        seen.add(n)
        data = sector(n)
        if len(data) < sector_size:
            break
        for at in range(0, sector_size, 128):
            length, kind = struct.unpack_from("<HB", data, at + 0x40)
            name = data[at:at + max(0, min(length, 64) - 2)].decode("utf-16-le", "replace")
            entries.append((name, kind, *struct.unpack_from("<3I", data, at + 0x44)))
        if n // per_fat >= len(fat_sectors):
            break
        if n // per_fat not in fats:
            fats[n // per_fat] = sector(fat_sectors[n // per_fat])
        fat = fats[n // per_fat]
        n = struct.unpack_from("<I", fat, n % per_fat * 4)[0] if len(fat) == sector_size else 0xFFFFFFFE
    if not entries or entries[0][1] != 5:
        return set()
    names: set[str] = set()
    pending, visited = [entries[0][4]], set()
    while pending:
        i = pending.pop()
        if i >= len(entries) or i in visited:
            continue
        visited.add(i)
        name, kind, left, right, _child = entries[i]
        if kind == 2:
            names.add(name)
        pending += [left, right]
    return names


def _is_binary_image(head: bytes) -> bool:
    if head.startswith((b"\xff\xd8\xff", b"\x89PNG\r\n\x1a\n", b"GIF87a", b"GIF89a", b"II*\x00", b"MM\x00*")):
        return True
    if head[:4] == b"RIFF" and head[8:12] == b"WEBP":
        return True
    if head[:2] == b"BM" and len(head) >= 18 and head[6:10] == b"\x00\x00\x00\x00":
        # "BM" alone is too weak: a text file can start "BMW"; reserved zeros and a DIB size are not text
        return struct.unpack_from("<I", head, 14)[0] in (12, 16, 40, 52, 56, 64, 108, 124)
    if head[:4] == b"\x00\x00\x01\x00" and len(head) >= 6:
        return struct.unpack_from("<H", head, 4)[0] > 0  # ICO with at least one image
    if head[4:8] == b"ftyp" and len(head) >= 16:
        box = min(struct.unpack_from(">I", head, 0)[0], len(head))
        brands = {head[8:12]} | {head[i:i + 4] for i in range(16, box - 3, 4)}
        return bool(brands & _IMAGE_BRANDS)
    return False
