"""Zip entry names the zip does not say how to read (Requirements 8.5, 8.6).

Ported test ideas from Gander's ZipNamesTest (MIT). An archive whose names are unmistakably one
script must come out right whatever the system language; the language only settles a tie.
"""

import codecs
import zipfile
import zlib
from pathlib import Path

import pytest

from sovander import zipnames
from sovander.zipnames import UTF8_FLAG, decode_names

FIXTURES = Path(__file__).resolve().parents[1] / "fixtures"
LANGUAGES = ("en", "ru", "zh", "ja", "")
RUSSIAN = ["Документы/Отчёт за квартал.txt", "Фото/Москва.txt"]


def raw_names(fixture: str) -> tuple[list[bytes], list[int]]:
    """Names as stored: zipfile decodes unflagged names as cp437, which round-trips every byte."""
    infos = zipfile.ZipFile(FIXTURES / fixture).infolist()
    raw = [i.orig_filename.encode("utf-8" if i.flag_bits & UTF8_FLAG else "cp437") for i in infos]
    return raw, [i.flag_bits for i in infos]


def legacy(names: list[str], encoding: str, lang: str = "en", **kwargs) -> tuple[str | None, list[str]]:
    return decode_names([n.encode(encoding) for n in names], [0] * len(names), lang=lang, **kwargs)


@pytest.mark.parametrize("lang", LANGUAGES)
@pytest.mark.parametrize(("fixture", "encoding", "expected"), [
    ("names-cp866.zip", "cp866", RUSSIAN),
    ("names-sjis.zip", "cp932", ["資料/報告書.txt", "資料/議事録.txt"]),
    ("names-gbk.zip", "gbk", ["季度报告/会议记录.txt", "照片/北京旅行.txt"]),
    ("names-mac.zip", "utf-8", ["Отчёт/报告 résumé.txt"]),  # macOS: UTF-8 without the flag
])
def test_windows_and_mac_names_read_right_whatever_the_language(fixture, encoding, expected, lang):
    assert decode_names(*raw_names(fixture), lang=lang) == (encoding, expected)


def test_names_that_say_what_they_are_need_no_encoding():
    encoding, names = decode_names(*raw_names("odd-names.zip"), lang="ru")
    assert encoding is None
    assert names == [
        "../escaped.txt", "/absolute/path.txt", "docs\\readme.txt", "unix\\name.txt", "a//b/./c.txt",
        "dup.txt", "dup.txt", "photo\u202egpj.apk",  # flagged UTF-8; making it safe to show is archive's job
    ]


@pytest.mark.parametrize("lang", LANGUAGES)
@pytest.mark.parametrize("encoding", ["cp866", "cp1251"])
def test_russian_names_read_as_russian_in_both_code_pages(encoding, lang):
    assert legacy(RUSSIAN, encoding, lang) == (encoding, RUSSIAN)


@pytest.mark.parametrize("encoding", ["cp866", "cp1251"])
@pytest.mark.parametrize("name", [
    "Отчёт.docx", "Москва", "Новая папка", "Счёт №5.pdf", "Їжак", "Гусь", "Подъезд",
    "Объявление.pdf", "Семья", "Мышь", "Бюджет 2026.xlsx", "ИП Иванов", "Акт",
])
def test_one_short_russian_name_reads_as_itself(name, encoding):
    """Least to go on: cp1251 read as cp866 is all letters too, but not as Russian spells them."""
    for lang in ("en", "ru"):
        assert legacy([name], encoding, lang)[1] == [name]


def test_western_names_are_not_read_as_cyrillic_on_a_russian_system():
    """cp437 read as cp866 is just as valid, but its accented letters sit inside Latin words."""
    names = ["Café menu/Crème brûlée.txt", "Café menu/Résumé.pdf"]
    for lang in ("ru", "en"):
        assert legacy(names, "cp437", lang)[1] == names


@pytest.mark.parametrize("name", [
    "Größe.xlsx", "Crème.docx", "Año 2024.xlsx", "Mañana.txt", "Señor.docx", "Björk.mp3",
    "Räkning.pdf", "Ação.pdf", "Più_bello.txt", "Irmã_2024.jpg", "MalmöFoto.jpg",
])
def test_a_western_name_is_not_read_as_chinese_or_japanese(name):
    """Two bytes to a character, "Größe" is "Gr批e": real characters, but wedged into a Latin word."""
    for lang in LANGUAGES:
        assert legacy([name], "cp850", lang)[1] == [name]


@pytest.mark.parametrize(("name", "encoding"), [
    ("テスト.txt", "cp932"), ("会議資料.docx", "cp932"), ("iPhone写真.jpg", "cp932"), ("A案.pptx", "cp932"),
    ("A社.pdf", "cp932"), ("Web用.docx", "cp932"), ("IT部.xlsx", "cp932"), ("工作总结.docx", "gbk"),
    ("PDF版.pdf", "gbk"), ("报告.pdf", "gbk"), ("照片", "gbk"), ("会议记录.txt", "gbk"),
    ("新建文件夹", "gbk"),
])
def test_a_cjk_name_reads_as_itself_even_on_a_russian_system(name, encoding):
    for lang in ("en", "ru", "zh", "ja"):
        assert legacy([name], encoding, lang)[1] == [name]


def test_a_chosen_encoding_replaces_the_guess():
    raw = ["季度报告/会议记录.txt".encode("gbk")]
    encoding, names = decode_names(raw, [0], override="cp1251", lang="zh")
    assert encoding == "cp1251"
    assert names == [raw[0].decode("cp1251", "replace")]


def test_korean_is_not_guessed_but_can_be_chosen():
    """Korean bytes are everyday GBK too; telling them apart needs Gander's frequency tables."""
    raw, flags = raw_names("names-korean.zip")
    assert decode_names(raw, flags, lang="en")[0] == "gbk"
    korean = ["문서/분기 보고서.pdf", "사진/제주도 여행.jpg"]
    assert decode_names(raw, flags, override="cp949") == ("cp949", korean)


def test_a_choice_never_rereads_a_name_flagged_as_utf8():
    flagged = "季度报告.txt".encode()
    gbk = "照片.txt".encode("gbk")
    encoding, names = decode_names([flagged, gbk], [UTF8_FLAG, 0], override="cp866")
    assert encoding == "cp866"
    assert names[0] == "季度报告.txt"


def test_flagged_and_unflagged_names_are_each_read_their_own_way():
    """7-Zip writes names that fit the code page in it and flags the rest as UTF-8."""
    raw = ["Отчёт.txt".encode(), *(n.encode("gbk") for n in ("季度报告/会议记录.txt", "照片/北京旅行.txt"))]
    assert decode_names(raw, [UTF8_FLAG, 0, 0], lang="en") == (
        "gbk", ["Отчёт.txt", "季度报告/会议记录.txt", "照片/北京旅行.txt"],
    )


def test_the_encoding_is_reported_only_where_a_name_needed_one():
    assert decode_names([b"notes.txt"], [0], lang="en")[0] is None
    assert decode_names(["Отчёт.txt".encode()], [UTF8_FLAG], lang="en")[0] is None
    assert decode_names(["Отчёт.txt".encode()], [0], lang="en")[0] == "utf-8"
    assert legacy(["季度报告.txt"], "gbk")[0] == "gbk"


def test_a_unicode_path_field_wins_while_its_checksum_matches():
    name = b"Otchet.txt"
    field = b"\x01" + zlib.crc32(name).to_bytes(4, "little") + "Отчёт.txt".encode()
    assert decode_names([name], [0], unicode_paths=[field])[1] == ["Отчёт.txt"]
    stale = b"\x01" + zlib.crc32(b"Renamed.txt").to_bytes(4, "little") + "Отчёт.txt".encode()
    assert decode_names([name], [0], unicode_paths=[stale])[1] == ["Otchet.txt"]


def test_bytes_no_candidate_can_read_still_come_out_as_a_name():
    encoding, names = decode_names([b"\x81\x7f\xff\x80"], [0], lang="en")
    assert encoding in zipnames.GUESSED
    assert len(names) == 1 and names[0]


def test_every_choice_is_a_real_encoding_under_one_name():
    assert zipnames.CHOICES[0] == "utf-8"
    assert len(set(zipnames.CHOICES)) == len(zipnames.CHOICES)
    assert set(zipnames.GUESSED) <= set(zipnames.CHOICES)
    for choice in zipnames.CHOICES:
        assert codecs.lookup(choice).name == choice


def test_the_system_language_comes_from_the_locale(monkeypatch):
    for var in ("LC_ALL", "LC_CTYPE", "LANG"):
        monkeypatch.delenv(var, raising=False)
    monkeypatch.setenv("LANG", "ru_RU.UTF-8")
    assert zipnames._system_language() == "ru"
    monkeypatch.setenv("LC_ALL", "C.UTF-8")  # the C locale says nothing; fall through to LANG
    assert zipnames._system_language() == "ru"
    monkeypatch.setenv("LC_ALL", "ja_JP.UTF-8")
    assert zipnames._system_language() == "ja"
