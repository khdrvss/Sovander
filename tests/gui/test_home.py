"""Home screen: recents with thumbnails, missing files, remove, clear, history off (Requirement 11)."""

import gc
import time
import weakref

import pytest
from gi.repository import Gdk, GdkPixbuf, Gio, GLib, Graphene, Gtk

from sovander import i18n, paths
from sovander.home import CARD_WIDTH, OPEN_ACTION, THUMB_HEIGHT, HomePage, _Card, when_opened
from sovander.store import Recent, Store, doc_key

try:
    import pymupdf as fitz
except ImportError:  # Ubuntu 22.04's python3-fitz has only the old name
    import fitz

pytestmark = pytest.mark.gui


@pytest.fixture(autouse=True)
def english():
    i18n.set_language("en")
    yield
    i18n.set_language("en")


@pytest.fixture
def store(tmp_path):
    return Store(tmp_path / "data")


@pytest.fixture
def docs(tmp_path, make_pdf, store):
    """Two recent files: a PDF (opened last) and a text file."""
    folder = tmp_path / "docs"
    folder.mkdir()
    text = folder / "notes.txt"
    text.write_text("hello")
    pdf = folder / "report.pdf"
    pdf.write_bytes(make_pdf(2))
    store.record_open(str(text), "notes.txt", "text", doc_key=doc_key(text))
    time.sleep(0.01)
    store.record_open(str(pdf), "report.pdf", "pdf", doc_key=doc_key(pdf))
    return {"pdf": pdf, "text": text}


@pytest.fixture
def home(window, store, wait):
    opened = []
    page = HomePage(store, opened.append)
    page.opened = opened
    window.set_child(page)
    window.present()
    assert wait(lambda: page.get_mapped())
    return page


def names(page):
    return [card.recent.display_name for card in page.cards]


def thumbs_folder():
    return paths.cache_dir() / "thumbs"


def menu_actions(card):
    model = card.menu_button.get_menu_model()
    actions = (model.get_item_attribute_value(i, "action", None) for i in range(model.get_n_items()))
    return [action.get_string() for action in actions]


# Listing (11.1, 11.4)


def test_recent_files_newest_first_with_dates(docs, home):
    assert home.stack.get_visible_child_name() == "recents"
    assert names(home) == ["report.pdf", "notes.txt"]
    assert all(card.get_child() is not None for card in home.cards)
    assert when_opened(time.time()).startswith("Today, ")


def test_dates_say_today_yesterday_or_the_date():
    now = time.mktime((2026, 10, 17, 15, 0, 0, 0, 0, -1))
    assert when_opened(now - 3600, now) == "Today, 14:00"
    assert when_opened(now - 86400, now) == "Yesterday, 15:00"
    assert "Today" not in when_opened(now - 10 * 86400, now)


def test_a_pdf_gets_its_thumbnail_and_text_keeps_an_icon(docs, home, wait):
    pdf_card, text_card = home.cards
    assert wait(lambda: pdf_card.shows_thumbnail)
    assert not text_card.shows_thumbnail
    assert len(list(thumbs_folder().glob("*.png"))) == 1


def test_activating_a_card_opens_its_file(docs, home):
    home.flow.emit("child-activated", home.cards[0])
    assert home.opened == [docs["pdf"]]


def test_the_menu_opens_and_removes(docs, home):
    card = home.cards[1]
    assert menu_actions(card) == ["home.open", "home.remove"]
    home.activate_action("home.open", GLib.Variant.new_string(str(docs["text"])))
    assert home.opened == [docs["text"]]


def test_a_missing_file_is_marked_and_does_not_open(docs, home, store):
    docs["text"].unlink()
    home.refresh()
    card = home.cards[1]
    assert card.recent.missing and card.path is None
    labels = []
    widget = card.get_child()
    stack = [widget]
    while stack:
        w = stack.pop()
        if isinstance(w, Gtk.Label):
            labels.append(w.get_label())
        child = w.get_first_child()
        while child is not None:
            stack.append(child)
            child = child.get_next_sibling()
    assert "Missing" in labels
    home.flow.emit("child-activated", card)
    assert home.opened == []
    assert menu_actions(card) == ["home.remove"]  # it can still be removed


def test_the_menu_key_opens_a_cards_menu(docs, home, wait):
    card = home.cards[0]
    card.grab_focus()
    assert home._on_key(None, Gdk.KEY_Menu, 0, 0)
    popover = card.menu_button.get_popover
    assert wait(lambda: popover() is not None and popover().get_visible())
    card.menu_button.popdown()


# Removing and clearing (11.3)


def test_remove_forgets_the_entry_and_its_thumbnail(docs, home, store, wait):
    assert wait(lambda: home.cards[0].shows_thumbnail)
    home.activate_action("home.remove", GLib.Variant.new_string(str(docs["pdf"])))
    assert names(home) == ["notes.txt"]
    assert [r.display_name for r in store.recents()] == ["notes.txt"]
    assert list(thumbs_folder().glob("*.png")) == []


def test_clear_history_asks_first(docs, home, store, wait):
    assert wait(lambda: home.cards[0].shows_thumbnail)
    dialog = home.ask_clear_history()
    assert dialog.get_transient_for() is home.get_root() and dialog.get_modal()
    dialog.cancel()
    assert len(store.recents()) == 2
    home.activate_action("home.clear", None)
    home.confirm_dialog.confirm()
    assert store.recents() == []
    assert list(thumbs_folder().glob("*.png")) == []
    assert home.stack.get_visible_child_name() == "empty"
    assert not home._actions.lookup_action("clear").get_enabled()


def test_escape_cancels_clear_history(docs, home, store, wait):
    dialog = home.ask_clear_history()
    assert wait(lambda: dialog.get_mapped())
    dialog.activate_action("window.close", None)
    assert wait(lambda: not dialog.get_visible())
    assert len(store.recents()) == 2


# Empty and history off (11.5)


def test_no_recents_shows_the_open_hint(home):
    assert home.stack.get_visible_child_name() == "empty"
    assert home._open_button.get_action_name() == OPEN_ACTION


def test_history_off_says_so_and_the_switch_turns_it_back_on(docs, home, store):
    home.keep_switch.set_active(False)
    assert store.settings.history_enabled is False
    assert home.stack.get_visible_child_name() == "off"
    assert home.cards == []
    home.keep_switch.set_active(True)
    assert store.settings.history_enabled is True
    assert names(home) == ["report.pdf", "notes.txt"]


def test_history_off_from_elsewhere_shows_on_refresh(docs, home, store):
    store.update_settings(history_enabled=False)
    home.refresh()
    assert home.stack.get_visible_child_name() == "off"
    assert not home.keep_switch.get_active()


# Language and lifetime


def test_labels_follow_the_language(docs, home):
    i18n.set_language("ru")
    assert home._title.get_label() == "Недавние файлы"
    assert home._clear.get_label() == "Очистить историю"
    assert names(home) == ["report.pdf", "notes.txt"]


def test_a_closed_home_page_is_freed(docs, store, wait):
    window = Gtk.Window()
    page = HomePage(store, lambda path: None)
    window.set_child(page)
    window.present()
    assert wait(page.get_mapped)
    page.ask_clear_history().cancel()
    gone = weakref.ref(page)
    del page
    window.destroy()
    del window
    for _ in range(3):
        wait(lambda: False, 0.1)
        gc.collect()
    assert gone() is None


# Even cards (C6)


def drawn(widget):
    """The widget as drawn now, its own style included, as an RGBA pixmap."""
    width, height = widget.get_width(), widget.get_height()
    snapshot = Gtk.Snapshot()
    Gtk.WidgetPaintable(widget=widget).snapshot(snapshot, width, height)
    texture = widget.get_native().get_renderer().render_texture(
        snapshot.to_node(), Graphene.Rect().init(0, 0, width, height)
    )
    return fitz.Pixmap(texture.save_to_png_bytes().get_data())


def test_a_tall_page_and_a_wide_slide_get_cards_of_one_size(tmp_path, window, wait):
    """Every thumbnail sits in the same box, scaled to fit and centred, so no card is taller."""
    flow = Gtk.FlowBox(selection_mode=Gtk.SelectionMode.NONE, valign=Gtk.Align.START)  # each at its own size
    cards = {}
    for name, (width, height) in {"tall.docx": (181, 256), "wide.pptx": (256, 144)}.items():
        png = tmp_path / f"{name}.png"
        picture = GdkPixbuf.Pixbuf.new(GdkPixbuf.Colorspace.RGB, False, 8, width, height)
        picture.fill(0x000080FF)
        picture.savev(str(png), "png", [], [])
        card = _Card(Recent((tmp_path / name).as_uri(), name, "text", time.time()), Gio.Menu())
        assert card.show_thumbnail(png)
        flow.append(card)
        cards[name] = card
    window.set_child(flow)
    window.present()
    tall, wide = cards["tall.docx"], cards["wide.pptx"]
    assert wait(lambda: tall._frame.get_height() > 0 and wide._frame.get_height() > 0)
    assert tall.measure(Gtk.Orientation.VERTICAL, -1)[:2] == wide.measure(Gtk.Orientation.VERTICAL, -1)[:2]
    assert tall.get_height() == wide.get_height()
    for card in (tall, wide):
        assert (card._frame.get_width(), card._frame.get_height()) == (CARD_WIDTH, THUMB_HEIGHT)

    def blue(card):
        """Where the picture is drawn in its box: (left, right) across the middle, (top, bottom) down it."""
        shot = drawn(card._frame)

        def is_blue(x, y):
            red, _green, blue = shot.pixel(x, y)[:3]
            return red < 60 and blue > 100

        across = [x for x in range(shot.width) if is_blue(x, shot.height // 2)]
        down = [y for y in range(shot.height) if is_blue(shot.width // 2, y)]
        return (across[0], across[-1]), (down[0], down[-1])

    (left, right), (top, bottom) = blue(tall)
    assert (top, bottom) == (0, THUMB_HEIGHT - 1) and abs(left - (CARD_WIDTH - 1 - right)) <= 1
    assert right - left + 1 == pytest.approx(THUMB_HEIGHT * 181 / 256, abs=2)
    (left, right), (top, bottom) = blue(wide)
    assert (left, right) == (0, CARD_WIDTH - 1) and abs(top - (THUMB_HEIGHT - 1 - bottom)) <= 1
    assert bottom - top + 1 == pytest.approx(CARD_WIDTH * 144 / 256, abs=2)
