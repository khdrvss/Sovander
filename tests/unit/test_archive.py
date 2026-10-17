"""Zip archives listed as folders and read in memory (Requirements 8, 10.4).

Test ideas ported from Gander's ZipReaderTest (MIT); the AES archives are also written here, by
a writer independent of the reader's chunked counter, and by 7-Zip when it is installed.
"""

import builtins
import hashlib
import hmac
import io
import os
import random
import shutil
import struct
import subprocess
import tracemalloc
import zipfile
import zlib
from pathlib import Path

import pytest
from cryptography.hazmat.primitives.ciphers import Cipher, algorithms, modes

from sovander import archive
from sovander.archive import Archive, Damaged, NeedsPassword, TooLarge, Unsupported, WrongPassword

FIXTURES = Path(__file__).resolve().parents[1] / "fixtures"
LOCKED = {  # locked.zip, password "gander": entry -> fixture it holds
    "open.txt": "plain.txt", "zipcrypto.txt": "plain.txt", "zipcrypto-trailing.md": "notes.md",
    "zipcrypto.png": "tiny.png", "aes128.txt": "plain.txt", "aes192.pdf": "six-pages.pdf",
    "aes256.png": "tiny.png",
}


def fixture(name: str) -> bytes:
    return (FIXTURES / name).read_bytes()


def named(a: Archive, path: str) -> archive.Entry:
    return next(e for e in a.entries if e.path == path)


def one_entry(name: bytes, body: bytes, *, size: int | None = None, crc: int | None = None, method: int = 0,
              flags: int = 0, extra: bytes = b"", made_by: int = 0x0314, external: int = 0) -> bytes:
    """A zip of one entry written as given, as Windows tools write them: any name bytes, any fields."""
    size = len(body) if size is None else size
    crc = zlib.crc32(body) if crc is None else crc
    local = struct.pack("<4s5H3I2H", b"PK\x03\x04", 20, flags, method, 0, 0x5C21, crc, len(body), size,
                        len(name), len(extra)) + name + extra + body
    central = struct.pack("<4s6H3I5H2I", b"PK\x01\x02", made_by, 20, flags, method, 0, 0x5C21, crc, len(body),
                          size, len(name), len(extra), 0, 0, 0, external, 0) + name + extra
    return local + central + struct.pack("<4s4H2IH", b"PK\x05\x06", 0, 0, 1, 1, len(central), len(local), 0)


def aes_zip(data: bytes, password: bytes = b"gander", *, strength: int = 3, version: int = 2,
            method: int = zipfile.ZIP_DEFLATED) -> bytes:
    """WinZip AES as its specification describes it, one counter block at a time."""
    key_size = 8 + 8 * strength
    salt = os.urandom(key_size // 2)
    keys = hashlib.pbkdf2_hmac("sha1", password, salt, 1000, 2 * key_size + 2)
    if method == zipfile.ZIP_DEFLATED:
        packer = zlib.compressobj(9, zlib.DEFLATED, -15)
        packed = packer.compress(data) + packer.flush()
    else:
        packed = data
    encrypt = Cipher(algorithms.AES(keys[:key_size]), modes.ECB()).encryptor().update
    stream = b"".join(encrypt(n.to_bytes(16, "little")) for n in range(1, len(packed) // 16 + 2))
    sealed = bytes(a ^ b for a, b in zip(packed, stream[:len(packed)], strict=True))
    code = hmac.new(keys[key_size:2 * key_size], sealed, hashlib.sha1).digest()[:10]
    extra = struct.pack("<2HH2sBH", 0x9901, 7, version, b"AE", strength, method)
    crc = 0 if version == 2 else zlib.crc32(data)
    return one_entry(b"secret.bin", salt + keys[-2:] + sealed + code, size=len(data), crc=crc, method=99,
                     flags=1, extra=extra)


# --- Listing (8.1) --------------------------------------------------------------------------------


def test_every_entry_is_listed_in_index_order():
    with Archive(FIXTURES / "archive.zip") as a:
        assert [e.path for e in a.entries] == [
            "reports", "reports/six-pages.pdf", "reports/notes.md", "photos/tiny.png",
            "photos/.hidden-thumbs", "__MACOSX/photos/._tiny.png", "plain.txt", "nested/stored.zip",
            "nested/packed.zip", "private/locked.txt", "private/table.dat",
        ]
        assert named(a, "reports").is_dir and not named(a, "plain.txt").is_dir
        # Sizes come from the index, even where the local header left them for after the data
        assert named(a, "reports/notes.md").size == len(fixture("notes.md"))


def test_an_archive_lists_like_a_folder():
    with Archive(FIXTURES / "archive.zip") as a:
        top = a.list()
        assert [(e.name, e.is_dir) for e in top] == [
            ("__MACOSX", True), ("nested", True), ("photos", True), ("private", True), ("reports", True),
            ("plain.txt", False),
        ]
        # "photos" has no entry of its own; its files imply it
        assert named_in(top, "photos").index == -1
        assert [e.name for e in a.list("reports")] == ["notes.md", "six-pages.pdf"]
        assert [e.name for e in a.list("/photos/")] == [".hidden-thumbs", "tiny.png"]
        assert named(a, "photos/.hidden-thumbs").hidden and named_in(top, "__MACOSX").hidden
        assert not named(a, "plain.txt").hidden
        assert a.list("nowhere") == []


def named_in(entries: list[archive.Entry], name: str) -> archive.Entry:
    return next(e for e in entries if e.name == name)


def test_names_that_climb_or_start_at_a_root_are_brought_in():
    with Archive(FIXTURES / "odd-names.zip") as a:
        assert [e.path for e in a.entries] == [
            "escaped.txt", "absolute/path.txt",
            "docs/readme.txt",  # written on Windows, where a backslash separates folders
            "unix\\name.txt",  # written on Unix, where it is part of a name
            "a/b/c.txt", "dup.txt", "dup.txt",
            "photo" + chr(0xFFFD) + "gpj.apk",  # the right-to-left override shown for what it is
        ]
        assert [e.name for e in a.list("a/b")] == ["c.txt"]
        # A repeated name is listed twice, and each reads its own bytes
        assert [a.read(e) for e in a.list() if e.name == "dup.txt"] == [b"first", b"second"]


def test_normalise_drops_empty_dot_and_dot_dot_segments():
    assert archive.normalise("/a/./b//../c/", dos=False) == "a/b/c"
    assert archive.normalise("..\\..\\win\\x", dos=True) == "win/x"
    assert archive.normalise("..\\..\\win\\x", dos=False) == "..\\..\\win\\x"
    assert archive.normalise("../..", dos=False) == ""
    assert archive.normalise("tab\there", dos=False) == "tab" + chr(0xFFFD) + "here"


def test_names_from_a_russian_windows_read_as_russian_and_can_be_reread():
    with Archive(FIXTURES / "names-cp866.zip", lang="en") as a:
        assert a.name_encoding == "cp866"
        assert [e.path for e in a.entries] == ["Документы/Отчёт за квартал.txt", "Фото/Москва.txt"]
        a.set_name_encoding("cp1251")
        assert a.name_encoding == "cp1251"
        assert "Фото/Москва.txt" not in [e.path for e in a.entries]
        a.set_name_encoding(None)
        assert [e.name for e in a.list()] == ["Документы", "Фото"]


def test_a_name_ending_in_half_a_shift_jis_character_is_not_a_folder():
    """表 is 95 5C in Shift_JIS: a backslash as the second half of a character, on a DOS host."""
    name = "予定表".encode("cp932")
    assert name.endswith(b"\\")
    with Archive(one_entry(name, b"hello", made_by=0x0014, external=0x20), lang="ja") as a:
        (entry,) = a.entries
        assert (entry.path, entry.is_dir) == ("予定表", False)
        assert a.read(entry) == b"hello"


def test_a_path_and_its_bytes_list_the_same():
    with Archive(FIXTURES / "archive.zip") as from_path:
        from_bytes = Archive(fixture("archive.zip"))
        assert from_path.entries == from_bytes.entries


def test_zip64_records_are_followed():
    with Archive(FIXTURES / "zip64.zip") as a:
        assert [e.path for e in a.entries] == ["big/report.pdf", "big/notes.txt"]
        assert a.read(named(a, "big/report.pdf")) == fixture("six-pages.pdf")


def test_something_in_front_of_the_zip_is_skipped():
    """A self-extracting stub, or a download that was prefixed: offsets are shifted, not wrong."""
    with Archive(b"MZ stub " * 100 + fixture("archive.zip")) as a:
        assert a.read(named(a, "plain.txt")) == fixture("plain.txt")


def test_a_comment_holding_the_end_signature_is_not_taken_for_the_end():
    out = io.BytesIO()
    with zipfile.ZipFile(out, "w") as z:
        z.writestr("a.txt", "alpha")
        z.comment = b"PK\x05\x06" + bytes(18) + b" trailing words"
    with Archive(out.getvalue()) as a:
        assert a.read(a.entries[0]) == b"alpha"


def test_junk_after_the_end_of_a_zip_is_tolerated():
    with Archive(fixture("odd-names.zip") + bytes(100)) as a:
        assert len(a.entries) == 8


# --- Reading (8.2, 8.3) ---------------------------------------------------------------------------


def test_entries_read_back_as_they_went():
    with Archive(FIXTURES / "archive.zip") as a:
        assert a.read(named(a, "reports/six-pages.pdf")) == fixture("six-pages.pdf")  # deflated
        assert a.read(named(a, "reports/notes.md")) == fixture("notes.md")  # sizes after the data
        assert a.read(named(a, "photos/tiny.png")) == fixture("tiny.png")  # stored
        assert a.read(named(a, "plain.txt")) == fixture("plain.txt")


@pytest.mark.parametrize("path", ["nested/stored.zip", "nested/packed.zip"])
def test_a_zip_inside_a_zip_opens_from_its_bytes(path):
    with Archive(FIXTURES / "archive.zip") as a:
        inner = Archive(a.read(named(a, path)))
        assert [e.path for e in inner.entries] == ["inside.txt"]


@pytest.mark.parametrize("method", [zipfile.ZIP_BZIP2, zipfile.ZIP_LZMA])
def test_bzip2_and_lzma_entries_read_back(method):
    text = "Отчёт за квартал\n".encode() * 5000
    out = io.BytesIO()
    with zipfile.ZipFile(out, "w", compression=method) as z:
        z.writestr("report.txt", text)
    with Archive(out.getvalue()) as a:
        assert a.read(a.entries[0]) == text


def test_reading_writes_nothing_anywhere(monkeypatch):
    """Requirement 8.3: once open, reading an entry opens no file at all."""
    with Archive(FIXTURES / "locked.zip") as a:
        def refuse(*args, **kwargs):
            raise AssertionError(f"tried to open {args[0]!r}")

        monkeypatch.setattr(builtins, "open", refuse)
        monkeypatch.setattr(os, "open", refuse)
        assert a.read(named(a, "aes192.pdf"), "gander") == fixture("six-pages.pdf")
        assert a.read(named(a, "zipcrypto.txt"), "gander") == fixture("plain.txt")


def test_a_folder_is_not_read():
    with Archive(FIXTURES / "archive.zip") as a, pytest.raises(ValueError):
        a.read(named(a, "reports"))


# --- Passwords (8.4) ------------------------------------------------------------------------------


@pytest.mark.parametrize("path", sorted(LOCKED))
def test_every_locked_entry_reads_back_with_its_password(path):
    with Archive(FIXTURES / "locked.zip") as a:
        assert a.read(named(a, path), "gander") == fixture(LOCKED[path])


@pytest.mark.parametrize("path", sorted(set(LOCKED) - {"open.txt"}))
def test_a_locked_entry_asks_for_a_password_and_refuses_a_wrong_one(path):
    with Archive(FIXTURES / "locked.zip") as a:
        entry = named(a, path)
        assert entry.encrypted and entry.supported
        with pytest.raises(NeedsPassword):
            a.read(entry)
        with pytest.raises(WrongPassword):
            a.read(entry, "goose")


@pytest.mark.parametrize("path", ["zipcrypto.txt", "zipcrypto.png"])
def test_a_wrong_password_that_passes_the_one_byte_check_is_still_wrong(path):
    """ZipCrypto checks one byte, so one wrong password in 256 passes; the checksum catches it."""
    with Archive(FIXTURES / "locked.zip") as a:
        entry = named(a, path)
        rec = a._records[entry.index]
        header = a._read(a._data_start(rec), 12)
        check = (rec.time >> 8) & 0xFF if rec.flags & 8 else rec.crc >> 24
        lucky = next(p for p in (f"wrong{n}" for n in range(100_000))
                     if archive._decrypt(header, archive._zipcrypto_keys(p.encode()))[11] == check)
        with pytest.raises(WrongPassword):
            a.read(entry, lucky)


def test_a_password_that_is_not_ascii_opens_in_the_code_page_it_was_typed_in():
    """ZipCrypto from a Russian Windows machine took the password in cp866; AES takes UTF-8."""
    with Archive(FIXTURES / "locked-cyrillic.zip") as a:
        for entry in a.entries:
            assert a.read(entry, "пароль") == fixture("plain.txt")
            with pytest.raises(WrongPassword):
                a.read(entry, "gander")


@pytest.mark.parametrize(("strength", "version", "method"), [
    (1, 1, zipfile.ZIP_DEFLATED), (2, 2, zipfile.ZIP_DEFLATED),
    (3, 2, zipfile.ZIP_STORED), (3, 1, zipfile.ZIP_STORED),
])
def test_winzip_aes_reads_back_past_the_first_256_counter_blocks(strength, version, method):
    data = random.Random(strength * 10 + version).randbytes(70_000)  # 4,375 blocks: the counter carries
    with Archive(aes_zip(data, strength=strength, version=version, method=method)) as a:
        assert a.read(a.entries[0], "gander") == data
        with pytest.raises(WrongPassword):
            a.read(a.entries[0], "goose")


def test_a_damaged_aes_entry_fails_its_authentication_code():
    sealed = bytearray(aes_zip(b"twelve bytes" * 100))
    sealed[30 + len(b"secret.bin") + 11 + 18 + 5] ^= 0x01  # local header, name, extra, salt+verifier
    with Archive(bytes(sealed)) as a, pytest.raises(Damaged):
        a.read(a.entries[0], "gander")


@pytest.mark.skipif(shutil.which("7z") is None, reason="7-Zip is not installed")
def test_aes_written_by_7zip_reads_back(tmp_path):
    """An independent writer: 7-Zip's own WinZip AES, over stored and deflated entries."""
    payload = tmp_path / "payload.bin"
    payload.write_bytes(random.Random(7).randbytes(300_000))
    (tmp_path / "words.txt").write_text("строка текста\n" * 20_000, encoding="utf-8")
    for level, mode in (("-mx=0", "-mem=AES256"), ("-mx=5", "-mem=AES128")):
        target = tmp_path / f"{level}.zip"
        subprocess.run(["7z", "a", "-tzip", mode, "-pgander", level, str(target), str(payload),
                        str(tmp_path / "words.txt")], check=True, capture_output=True)
        with Archive(target) as a:
            for entry in a.entries:
                assert a.read(entry, "gander") == (tmp_path / entry.name).read_bytes()


# --- What is not read (10.4) ----------------------------------------------------------------------


def test_deflate64_and_strong_encryption_are_listed_and_not_read():
    with Archive(FIXTURES / "deflate64.zip") as a:
        assert [e.path for e in a.entries] == ["long.txt", "short.txt", "empty.txt"]
        for entry in a.entries:
            assert not entry.supported
            with pytest.raises(Unsupported):
                a.read(entry)
    with Archive(FIXTURES / "locked.zip") as a:
        for path in ("strong.bin", "aes256-deflate64.md"):
            assert named(a, path).encrypted and not named(a, path).supported
            with pytest.raises(Unsupported):
                a.read(named(a, path), "gander")


def test_too_many_entries_is_refused():
    records = b"".join(
        struct.pack("<4s6H3I5H2I", b"PK\x01\x02", 20, 20, 0, 0, 0, 0x21, 0, 0, 0, 1, 0, 0, 0, 0, 0, 0) + b"a"
        for _ in range(archive.MAX_ENTRIES + 1)
    )
    # A 16-bit count that wrapped, as a writer with no ZIP64 record leaves it: the index is walked anyway
    count = (archive.MAX_ENTRIES + 1) & 0xFFFF
    end = struct.pack("<4s4H2IH", b"PK\x05\x06", 0, 0, count, count, len(records), 0, 0)
    with pytest.raises(TooLarge):
        Archive(records + end)


def test_an_index_too_large_to_hold_is_refused_unread(monkeypatch):
    monkeypatch.setattr(archive, "MAX_INDEX_BYTES", 100)
    with pytest.raises(TooLarge):
        Archive(FIXTURES / "archive.zip")


def test_an_entry_over_2_gb_expanded_is_refused_before_it_is_read():
    data = one_entry(b"huge.bin", b"tiny", size=archive.MAX_ENTRY_BYTES + 1, method=8)
    with Archive(data) as a:
        assert a.entries[0].size == archive.MAX_ENTRY_BYTES + 1
        with pytest.raises(TooLarge):
            a.read(a.entries[0])


def test_an_entry_that_expands_suspiciously_far_is_refused():
    bomb = one_entry(b"bomb.bin", bytes(1000), size=archive.RATIO_FLOOR + 1, method=8)
    with Archive(bomb) as a, pytest.raises(TooLarge):
        a.read(a.entries[0])


@pytest.mark.parametrize("method", [zipfile.ZIP_DEFLATED, zipfile.ZIP_BZIP2])
def test_an_entry_larger_than_its_index_says_is_stopped_not_inflated(method):
    """A bomb that declares 100 bytes and holds 20 MB: inflation stops one byte past 100."""
    out = io.BytesIO()
    with zipfile.ZipFile(out, "w", compression=method) as z:
        z.writestr("bomb.txt", bytes(20_000_000))
    data = bytearray(out.getvalue())
    info = zipfile.ZipFile(io.BytesIO(bytes(data))).infolist()[0]
    central = data.rindex(b"PK\x01\x02")
    struct.pack_into("<I", data, central + 24, 100)  # the uncompressed size
    struct.pack_into("<I", data, info.header_offset + 22, 100)
    with Archive(bytes(data)) as a:
        tracemalloc.start()
        try:
            with pytest.raises(Damaged):
                a.read(a.entries[0])
            peak = tracemalloc.get_traced_memory()[1]
        finally:
            tracemalloc.stop()
    assert peak < 1_000_000


# --- Damage ---------------------------------------------------------------------------------------


# Not archive.zip cut short: it holds a whole zip stored inside, which is then the last zip in the
# file and is what gets listed. The format gives nothing else to go on (Gander says the same).
@pytest.mark.parametrize("data", [
    b"", b"hello, not a zip", fixture("odd-names.zip")[:-10], fixture("six-pages.pdf"),
])
def test_what_is_not_a_whole_zip_is_damaged(data):
    with pytest.raises(Damaged):
        Archive(data)


def test_an_entry_that_fails_its_checksum_is_damaged():
    with Archive(one_entry(b"a.txt", b"hello", crc=12345)) as a, pytest.raises(Damaged):
        a.read(a.entries[0])


def test_an_entry_whose_bytes_are_not_its_method_is_damaged():
    """archive.zip's table.dat claims LZMA over bytes that are not."""
    with Archive(FIXTURES / "archive.zip") as a:
        entry = named(a, "private/table.dat")
        assert entry.supported
        with pytest.raises(Damaged):
            a.read(entry)


def test_an_index_pointing_at_no_entry_is_damaged():
    data = bytearray(one_entry(b"a.txt", b"hello"))
    data[0:4] = b"XXXX"
    with Archive(bytes(data)) as a, pytest.raises(Damaged):
        a.read(a.entries[0])


def test_a_missing_file_is_an_os_error():
    with pytest.raises(FileNotFoundError):
        Archive(FIXTURES / "no-such.zip")
