"""Which code page a zip's entry names were written in (Requirements 8.5, 8.6).

Ported from Gander's ZipNames.kt (MIT). A zip marks UTF-8 names with flag bit 11 and says nothing
about the rest. Windows writes them in the machine's DOS code page (cp866 on a Russian machine,
cp932 on a Japanese one, gbk on a Chinese one) and macOS writes UTF-8 without the flag. Strict
decoding cannot choose, since GBK bytes decode cleanly under most code pages, so each candidate
is scored on how much of its reading is text people write, over all the names at once, and the
system language settles a tie.
"""

import codecs
import os
import unicodedata
import zlib

UTF8_FLAG = 0x800

# Guessed among, in this order on a tie unless the language says otherwise. Cyrillic first: the
# Viewer is for Russian and English speakers, and a short Chinese name that ties with a Cyrillic
# reading is rare once _letter_outside_ascii_word has had its say.
GUESSED = ("cp866", "cp1251", "gbk", "cp932", "cp437", "cp850")

# What a reader can pick by hand (8.6): UTF-8, the guessed ones, then code pages that are never
# guessed because they read most bytes as some other language's letters (Big5 and Korean among
# them: telling those from GBK needs character frequency tables this port leaves out).
CHOICES = (
    "utf-8", "cp866", "cp1251", "cp437", "cp850", "gbk", "cp932", "cp950", "cp949",
    "cp852", "cp737", "cp857", "cp862", "cp720", "cp874", "cp775", "cp1258",
)

_EAST_SLAVIC = set("ЁёІіЇїЄєҐґЎў")
_VOWELS = set("аеёиоуыэюяАЕЁИОУЫЭЮЯ")
_SCRIPT = {
    "gbk": "gb", "cp932": "sjis", "cp866": "cyrillic", "cp1251": "cyrillic",
    "cp437": "latin", "cp850": "latin",
}
_SAMPLE_BYTES = 64 * 1024


def decode_names(
    raw_names: list[bytes],
    flags: list[int],
    override: str | None = None,
    lang: str | None = None,
    unicode_paths: list[bytes | None] | None = None,
) -> tuple[str | None, list[str]]:
    """Decode every name of one archive; returns (encoding used for unflagged names, names).

    The encoding is None when no name needed one (all flagged, ASCII or with a Unicode Path
    field), which is also when there is nothing for a reader to override. `override` replaces
    the guess for those names only: a name the writer flagged as UTF-8 is never reread.
    `lang` (e.g. "ru") breaks ties; None reads it from the locale environment.
    """
    paths = unicode_paths or [None] * len(raw_names)
    said = [_said(raw, flag, path) for raw, flag, path in zip(raw_names, flags, paths, strict=True)]
    pairs = list(zip(raw_names, said, strict=True))
    legacy = [raw for raw, name in pairs if name is None]
    if not legacy:
        encoding = None
    elif override:
        encoding = codecs.lookup(override).name
    elif all(_strict(raw, "utf-8") is not None for raw in legacy):
        # macOS: UTF-8 without the flag. Checked over every name, since one short name can be
        # valid UTF-8 by accident and a whole archive of them is not.
        encoding = "utf-8"
    else:
        encoding = _guess(legacy, _system_language() if lang is None else lang)
    names = [name if name is not None else raw.decode(encoding, "replace") for raw, name in pairs]
    return encoding, names


def _said(raw: bytes, flag: int, unicode_path: bytes | None) -> str | None:
    """The name when the archive itself says how to read it, else None."""
    if flag & UTF8_FLAG:
        return raw.decode("utf-8", "replace")
    from_field = _unicode_path(raw, unicode_path)
    if from_field is not None:
        return from_field
    if raw.isascii():
        return raw.decode("ascii")
    return None


def _unicode_path(raw: bytes, field: bytes | None) -> str | None:
    """Info-ZIP's Unicode Path field (0x7075), if its CRC still matches the name beside it."""
    if not field or len(field) < 5 or field[0] != 1:
        return None
    if int.from_bytes(field[1:5], "little") != zlib.crc32(raw):
        return None  # left behind by a tool that renamed the entry
    return _strict(field[5:], "utf-8")


def _strict(raw: bytes, encoding: str) -> str | None:
    try:
        return raw.decode(encoding)
    except UnicodeDecodeError:
        return None


def _system_language() -> str:
    for var in ("LC_ALL", "LC_CTYPE", "LANG"):
        value = os.environ.get(var, "")
        if value and value != "POSIX" and not value.startswith("C"):
            return value.split(".")[0].split("_")[0].lower()
    return ""


def _order(lang: str) -> tuple[str, ...]:
    first = {"zh": ("gbk",), "ja": ("cp932",)}.get(lang, ())
    return tuple(dict.fromkeys(first + GUESSED))


def _guess(names: list[bytes], lang: str) -> str:
    """The best-scoring candidate; on a tie, the first for this language."""
    sample = bytearray()
    for name in names:
        if sample and len(sample) + len(name) > _SAMPLE_BYTES:
            break
        # A newline cannot fall inside a character in any candidate
        sample += name + b"\n"
    best, best_score = "", -2.0
    for encoding in _order(lang):
        score = _score(encoding, bytes(sample))
        if score > best_score:
            best, best_score = encoding, score
    return best


def _score(encoding: str, sample: bytes) -> float:
    """How much of `sample` read as `encoding` is ordinary text, 0..1, or -1 if it is invalid.

    Only characters outside ASCII count: ASCII reads the same in every candidate.
    """
    text = _strict(sample, encoding)
    if text is None:
        return -1.0
    script = _SCRIPT[encoding]
    counted, total = 0, 0.0
    for at, ch in enumerate(text):
        if ch < "\x80":
            continue
        counted += 1
        if script in ("gb", "sjis"):
            own = _own_bytes(ch, encoding)
            if own is not None and _western_letter_paired_up(text, at, own):
                continue
            total += _gb(own) if script == "gb" else _sjis(ch, own)
        elif script == "cyrillic":
            total += _letter_outside_ascii_word(text, at, "CYRILLIC")
        elif _script_letter(ch, "LATIN"):
            total += 1.0
    return total / counted if counted else 0.0


def _own_bytes(ch: str, encoding: str) -> bytes | None:
    try:
        return ch.encode(encoding)
    except UnicodeEncodeError:
        return None


def _gb(own: bytes | None) -> float:
    """GB2312 level 1 is the hanzi in everyday use; GBK's additions are where other code pages land."""
    if own is None or len(own) != 2 or not 0xA1 <= own[1] <= 0xFE:
        return 0.0
    lead = own[0]
    if 0xB0 <= lead <= 0xD7:
        return 1.0
    if 0xD8 <= lead <= 0xF7 or lead in (0xA1, 0xA3):
        return 0.5
    return 0.0  # kana, Greek, Cyrillic, box drawing: in GB2312, never in a Chinese name


def _sjis(ch: str, own: bytes | None) -> float:
    """Kana is Japanese and nothing else; kanji split at JIS level 1 like the hanzi above."""
    if "\u3041" <= ch <= "\u30ff":
        return 1.0
    if own is None:
        return 0.0
    if len(own) == 1:
        return 0.25  # half-width katakana: genuine, from an old machine
    code = own[0] << 8 | own[1]
    if 0x889F <= code <= 0x9872:
        return 1.0
    if 0x989F <= code <= 0x9FFC or 0xE040 <= code <= 0xEAA4:
        return 0.5  # JIS level 2 kanji
    if 0x8140 <= code <= 0x81FC or 0x824F <= code <= 0x829A:
        return 0.5  # punctuation, full-width digits and letters
    return 0.0


def _script_letter(ch: str, script: str) -> bool:
    return ch.isalpha() and unicodedata.name(ch, "").startswith(script)


def _ascii_letter(ch: str) -> bool:
    return ch.isascii() and ch.isalpha()


def _neighbours(text: str, at: int) -> tuple[str, str]:
    return (text[at - 1] if at > 0 else "", text[at + 1] if at + 1 < len(text) else "")


def _letter_outside_ascii_word(text: str, at: int, script: str) -> float:
    """1 for a letter of `script` not inside a word of ASCII letters, half for a rare one.

    café in cp437 reads under cp866 as "cafВ", every character a letter; where it sits gives it away.
    """
    ch = text[at]
    if not _script_letter(ch, script):
        return 0.0
    before, after = _neighbours(text, at)
    if _ascii_letter(before) or _ascii_letter(after):
        return 0.0
    # Spelling that Russian never has: a hard or soft sign or ы starting a word or after a vowel,
    # a hard sign ending one. What tells a cp1251 name read as cp866 ("Ёлка" as "иыър") apart.
    if ch in "ъыьЪЫЬ" and (before in _VOWELS or not _script_letter(before or " ", script)):
        return 0.0
    if ch in "ъЪ" and not _script_letter(after or " ", script):
        return 0.0
    # GBK read as cp1251 lands on Serbian and Macedonian letters (ј, Ѕ, Љ) as often as not, which
    # is what keeps a short Chinese name from reading as Cyrillic; Gander used frequency tables.
    return 1.0 if "\u0410" <= ch <= "\u044f" or ch in _EAST_SLAVIC else 0.5


def _western_letter_paired_up(text: str, at: int, own: bytes) -> bool:
    """Whether a double-byte character is a Western accented letter paired with the next one.

    "Größe" read two bytes to a character is "Gr批e": as everyday as the letters. It gives itself
    away by standing alone against an ASCII word while its bytes, read the Western way, spell an
    accented letter and what follows one in a word.
    """
    before, after = _neighbours(text, at)
    if before >= "\x80" or after >= "\x80":
        return False  # a run of such characters is how CJK is written
    if not (_ascii_letter(before) or _ascii_letter(after)):
        return False
    return any(_spells_western(own.decode(cp), text, at) for cp in ("cp850", "cp437"))


def _cased_latin(ch: str) -> bool:
    return unicodedata.category(ch) in ("Lu", "Ll") and unicodedata.name(ch, "").startswith("LATIN")


def _spells_western(letters: str, text: str, at: int) -> bool:
    if not letters or len(letters) > 2 or not _cased_latin(letters[0]):
        return False
    first, second = letters[0], letters[1:]
    if second and not _cased_latin(second) and second != "_":
        return False
    # A grave on u or i ends a word in Italian and French: "Webùp" is Web用
    if first in "ùì" and second.islower():
        return False
    before = text[at - 1] if at > 0 and _ascii_letter(text[at - 1]) else ""
    in_capitals = before.isupper() and at > 1 and _ascii_letter(text[at - 2]) and text[at - 2].isupper()
    if first.islower() and in_capitals:
        return False  # "ITòö" is IT部
    if first.isupper() and second.islower() and before.isupper():
        return False  # "AÄð" is A社
    if first.islower() and second >= "\x80" and second.isupper():
        return False  # "WordãÃ" is Word판
    return True
