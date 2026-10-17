"""The book and Markdown pages' own words follow the app's language (Requirement 14, task 6.4)."""

import re
from pathlib import Path

import pytest

from sovander import i18n
from sovander.views.web_words import page_words
from tests.gui import test_web_view as web

build_epub, js, opened = web.build_epub, web.js, web.opened  # opened is a fixture

pytestmark = pytest.mark.gui

ASSETS = Path(__file__).resolve().parents[2] / "data" / "assets"
SAID = re.compile(
    r'\b(?:vwSay|vwProblem|vwError|vwFail|problem)\(\s*"([^"]*)"(?:\s*,\s*"([^"]*)")?|data-vw-word="([^"]*)"'
)
LABELS = """return ['epub-contents-open', 'epub-prev', 'epub-next', 'epub-contents-title',
  'epub-contents-close'].map(id => document.getElementById(id).textContent).concat([document.title]);"""
ENGLISH = ["Contents", "Previous", "Next", "Contents", "Close", "Book"]
PARTS = "return [...document.querySelectorAll('#epub-contents-list li')].map(l => l.textContent);"


@pytest.fixture(autouse=True)
def english():
    i18n.set_language("en")
    yield
    i18n.set_language("en")


def test_every_word_the_pages_say_is_in_the_dictionary_and_back():
    said = set()
    for path in [*ASSETS.glob("*.js"), *ASSETS.glob("*.html")]:
        for match in SAID.finditer(path.read_text(encoding="utf-8")):
            said |= {word for word in match.groups() if word}
    assert said == set(page_words())
    assert all(english == shown for english, shown in page_words().items())
    i18n.set_language("ru")
    assert all(english != shown for english, shown in page_words().items())


def test_book_buttons_and_contents_switch_language(opened, wait, fixture_file):
    view = opened("epub", fixture_file("harbour.epub").read_bytes())
    assert js(view, wait, LABELS) == ENGLISH
    i18n.set_language("ru")
    russian = ["Оглавление", "Предыдущая", "Следующая", "Оглавление", "Закрыть", "Книга"]
    assert wait(lambda: js(view, wait, LABELS) == russian, 5), js(view, wait, LABELS)
    view.show_contents()
    assert wait(lambda: js(view, wait, "return !document.getElementById('epub-contents').hidden;"), 5)
    i18n.set_language("en")
    assert wait(lambda: js(view, wait, LABELS) == ENGLISH, 5)


def test_a_book_opened_in_russian_says_russian_from_the_start(opened, wait):
    i18n.set_language("ru")
    view = opened("epub", build_epub("http://127.0.0.1:9"))  # no contents of its own: parts by number
    assert js(view, wait, PARTS) == ["Часть 1"]
    assert js(view, wait, LABELS)[0] == "Оглавление"
    i18n.set_language("en")
    assert wait(lambda: js(view, wait, PARTS) == ["Part 1"], 5)


def test_an_error_card_and_the_apps_note_follow_the_language(opened, wait):
    i18n.set_language("ru")
    view = opened("epub", b"not a zip at all", ready=False)
    assert wait(lambda: any(e[0] == "error" for e in view.events), 20)
    assert [e for e in view.events if e[0] == "error"][-1][1] == (
        "Не удалось открыть книгу: Это не EPUB, или файл повреждён.")
    i18n.set_language("en")
    assert wait(lambda: [e for e in view.events if e[0] == "error"][-1][1] == (
        "Could not open this book: It is not an EPUB, or the file is damaged."), 5)
