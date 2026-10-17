import ast
import gettext
import io
import os
import re
import shutil
from pathlib import Path

import pytest

from sovander import i18n, paths
from sovander.i18n import PoEntry, compile_entries, compile_po, parse_po

ROOT = Path(__file__).resolve().parents[2]
PO = ROOT / "po"
PLACEHOLDER = re.compile(r"\{[^{}]*\}")


@pytest.fixture(autouse=True)
def fresh_state(monkeypatch):
    """Each test starts with no language chosen and no listeners."""
    monkeypatch.setattr(i18n, "_language", None)
    monkeypatch.setattr(i18n, "_translation", gettext.NullTranslations())
    monkeypatch.setattr(i18n, "_listeners", [])


def entries(name):
    return [e for e in parse_po((PO / name).read_text(encoding="utf-8")) if e.msgid]


def header(name):
    (head,) = [e for e in parse_po((PO / name).read_text(encoding="utf-8")) if not e.msgid]
    return head.msgstr[0]


def ident(e: PoEntry):
    return (e.msgctxt, e.msgid, e.msgid_plural)


# Catalog completeness (14.1, 14.5)


def test_every_russian_entry_is_translated_and_not_fuzzy():
    ru = entries("ru.po")
    assert ru
    untranslated = [e.msgid for e in ru if not all(s.strip() for s in e.msgstr)]
    fuzzy = [e.msgid for e in ru if e.fuzzy]
    assert untranslated == [] and fuzzy == []


def test_russian_catalog_matches_the_template():
    assert sorted(map(ident, entries("ru.po")), key=str) == sorted(
        map(ident, entries("sovander.pot")), key=str
    )


def test_russian_header():
    head = header("ru.po")
    assert "charset=UTF-8" in head and "Language: ru" in head
    assert "nplurals=3;" in head
    assert all(len(e.msgstr) == 3 for e in entries("ru.po") if e.msgid_plural)


def test_translations_keep_placeholders():
    for e in entries("ru.po"):
        expected = set(PLACEHOLDER.findall(e.msgid)) | set(PLACEHOLDER.findall(e.msgid_plural or ""))
        for form in e.msgstr:
            assert set(PLACEHOLDER.findall(form)) == expected, e.msgid


def test_every_string_in_the_code_is_in_the_template():
    in_template = set(map(ident, entries("sovander.pot")))
    missing = sorted(code_messages() - in_template, key=str)
    assert not missing, "add to po/sovander.pot and po/ru.po:\n" + "\n".join(map(repr, missing))


def test_linguas_lists_every_translation():
    listed = {
        line.strip()
        for line in (PO / "LINGUAS").read_text().splitlines()
        if line.strip() and not line.startswith("#")
    }
    assert listed == {p.stem for p in PO.glob("*.po")} == {"ru"}
    assert set(i18n.LANGUAGES) == listed | {i18n.SOURCE_LANGUAGE}


def code_messages():
    """(context, msgid, plural) of every literal passed to _, N_, ngettext and pgettext in sovander/."""
    found = set()
    for path in (ROOT / "sovander").rglob("*.py"):
        for node in ast.walk(ast.parse(path.read_text(encoding="utf-8"))):
            if not isinstance(node, ast.Call):
                continue
            func = node.func
            name = (
                func.id
                if isinstance(func, ast.Name)
                else func.attr
                if isinstance(func, ast.Attribute)
                else None
            )
            if isinstance(func, ast.Attribute) and not (
                isinstance(func.value, ast.Name) and func.value.id == "i18n"
            ):
                continue
            texts = [
                a.value if isinstance(a, ast.Constant) and isinstance(a.value, str) else None
                for a in node.args
            ]
            if name in ("_", "N_") and texts[:1] and texts[0]:
                found.add((None, texts[0], None))
            elif name == "ngettext" and len(texts) >= 2 and texts[0] and texts[1]:
                found.add((None, texts[0], texts[1]))
            elif name == "pgettext" and len(texts) >= 2 and texts[0] and texts[1]:
                found.add((texts[0], texts[1], None))
    return found


# Switching (14.2-14.4)


def test_switching_language_changes_translations():
    assert i18n.set_language("en") == "en"
    assert i18n._("No matches") == "No matches"
    i18n.set_language("ru")
    assert i18n.language() == "ru"
    assert i18n._("No matches") == "Совпадений нет"
    assert i18n.pgettext("zip archive", "Name Encoding") == "Кодировка имён"
    i18n.set_language("en")
    assert i18n._("No matches") == "No matches"


@pytest.mark.parametrize(
    ("n", "form"), [(1, "страница"), (3, "страницы"), (5, "страниц"), (11, "страниц"), (21, "страница")]
)
def test_russian_plurals(n, form):
    i18n.set_language("ru")
    assert i18n.ngettext("{n} page", "{n} pages", n).format(n=n) == f"{n} {form}"


def test_unknown_string_falls_back_to_english():
    i18n.set_language("ru")
    assert i18n._("Not in any catalog") == "Not in any catalog"


def test_unsupported_language_is_refused():
    with pytest.raises(ValueError):
        i18n.set_language("de")


def test_listeners_hear_changes_only():
    heard = []
    i18n.set_language("en")
    i18n.add_listener(heard.append)
    i18n.set_language("en")
    i18n.set_language("ru")
    i18n.set_language("en")
    assert heard == ["ru", "en"]
    i18n.remove_listener(heard.append)
    i18n.set_language("ru")
    assert heard == ["ru", "en"]


def test_a_dead_widget_stops_listening():
    class Widget:
        def __init__(self):
            self.seen = []

        def relabel(self, language):
            self.seen.append(language)

    i18n.set_language("en")
    widget = Widget()
    i18n.add_listener(widget.relabel)
    i18n.set_language("ru")
    assert widget.seen == ["ru"]
    del widget
    i18n.set_language("en")
    assert i18n._listeners == []


def test_a_broken_listener_does_not_stop_the_others():
    heard = []

    def broken(_language):
        raise RuntimeError("gone")

    i18n.set_language("en")
    i18n.add_listener(broken)
    i18n.add_listener(heard.append)
    i18n.set_language("ru")
    assert heard == ["ru"]


def test_first_use_picks_the_system_language(monkeypatch):
    monkeypatch.setenv("LANG", "ru_RU.UTF-8")
    monkeypatch.delenv("LANGUAGE", raising=False)
    monkeypatch.delenv("LC_ALL", raising=False)
    monkeypatch.delenv("LC_MESSAGES", raising=False)
    assert i18n._("No matches") == "Совпадений нет"
    assert i18n.language() == "ru"


@pytest.mark.parametrize(
    ("env", "expected"),
    [
        ({"LANG": "ru_RU.UTF-8"}, "ru"),
        ({"LANG": "ru_UA.UTF-8"}, "ru"),
        ({"LANG": "en_GB.UTF-8"}, "en"),
        ({"LANG": "de_DE.UTF-8"}, "en"),
        ({"LANG": "fr_FR.UTF-8", "LANGUAGE": "fr:en"}, "en"),
        ({"LANG": "de_DE.UTF-8", "LANGUAGE": "de:ru"}, "ru"),
        ({"LANG": "en_US.UTF-8", "LC_MESSAGES": "ru_RU.UTF-8"}, "ru"),
        ({"LANG": "ru_RU.UTF-8", "LC_ALL": "en_US.UTF-8"}, "en"),
        ({"LANG": "C.UTF-8", "LANGUAGE": "ru"}, "en"),
        ({"LANG": "sr_RS@latin"}, "en"),
        ({}, "en"),
    ],
)
def test_system_language(env, expected):
    assert i18n.system_language(env) == expected


# Compiling


def test_development_catalog_is_compiled_into_the_cache_folder():
    i18n.set_language("ru")
    mo = paths.cache_dir() / "locale" / "ru" / "LC_MESSAGES" / "sovander.mo"
    assert mo.is_file()
    assert gettext.GNUTranslations(io.BytesIO(mo.read_bytes())).gettext("No matches") == "Совпадений нет"


def test_edited_po_is_compiled_again(tmp_path, monkeypatch):
    po_dir = tmp_path / "po"
    shutil.copytree(PO, po_dir)
    monkeypatch.setattr(i18n, "PO_DIR", po_dir)
    i18n.set_language("ru")
    ru = po_dir / "ru.po"
    ru.write_text(
        ru.read_text(encoding="utf-8").replace("Совпадений нет", "Ничего не найдено"), encoding="utf-8"
    )
    future = ru.stat().st_mtime + 10
    os.utime(ru, (future, future))
    i18n.set_language("en")
    i18n.set_language("ru")
    assert i18n._("No matches") == "Ничего не найдено"


def test_installed_catalog_is_used_without_a_source_tree(tmp_path, monkeypatch):
    monkeypatch.setattr(i18n, "PO_DIR", tmp_path / "no-po-here")
    found = []
    monkeypatch.setattr(
        i18n._gettext, "translation", lambda *a, **k: found.append((a, k)) or gettext.NullTranslations()
    )
    i18n.set_language("ru")
    assert found and found[0][0] == ("sovander",) and found[0][1]["languages"] == ["ru"]


def test_compiler_leaves_out_fuzzy_and_untranslated():
    po = r"""
msgid ""
msgstr "Content-Type: text/plain; charset=UTF-8\n"

msgid "kept"
msgstr "оставлено"

#, fuzzy
msgid "guessed"
msgstr "угадано"

msgid "empty"
msgstr ""

#~ msgid "obsolete"
#~ msgstr "устарело"

msgctxt "menu"
msgid "File"
msgstr "Файл"

msgid "multi"
"line"
msgstr "много"
"строк \"в кавычках\"\n"
"""
    t = gettext.GNUTranslations(io.BytesIO(compile_po(po)))
    assert t.gettext("kept") == "оставлено"
    assert t.gettext("guessed") == "guessed"
    assert t.gettext("empty") == "empty"
    assert t.gettext("obsolete") == "obsolete"
    assert t.pgettext("menu", "File") == "Файл"
    assert t.gettext("multiline") == 'многострок "в кавычках"\n'


def test_entries_without_blank_lines_between_them():
    parsed = parse_po('msgid "a"\nmsgstr "A"\nmsgid "b"\nmsgstr "B"\n#, fuzzy\nmsgid "c"\nmsgstr "C"\n')
    assert [(e.msgid, e.msgstr, e.fuzzy) for e in parsed] == [
        ("a", ["A"], False),
        ("b", ["B"], False),
        ("c", ["C"], True),
    ]


def test_bad_po_line_is_reported():
    with pytest.raises(ValueError, match="line 2"):
        parse_po('msgid "a"\nmsgstr nonsense\n')


def test_empty_catalog_compiles():
    assert gettext.GNUTranslations(io.BytesIO(compile_entries([]))).gettext("x") == "x"
