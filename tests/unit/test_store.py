import hashlib
import itertools
import json
import os

import pytest

from sovander import paths, store
from sovander.store import POSITIONS_MAX, RECENTS_MAX, Forgotten, Settings, Store, doc_key


@pytest.fixture(autouse=True)
def clock(monkeypatch):
    """A clock that ticks once per call, so 'newest' never ties."""
    ticks = itertools.count(1_000_000)
    monkeypatch.setattr(store, "_now", lambda: float(next(ticks)))


@pytest.fixture
def folder(tmp_path):
    return tmp_path / "data"


def make_file(tmp_path, name, data=b"hello"):
    path = tmp_path / name
    path.write_bytes(data)
    return path


# Keys


def test_doc_key_is_gander_scheme(tmp_path):
    data = os.urandom(70_000)
    path = make_file(tmp_path, "a.pdf", data)
    expected = hashlib.sha256(data[:65536] + str(len(data)).encode()).hexdigest()[:32]
    assert doc_key(path) == expected
    assert doc_key(data) == expected


def test_doc_key_follows_content_not_name(tmp_path):
    a = make_file(tmp_path, "a.pdf", b"same bytes")
    b = make_file(tmp_path, "renamed copy.pdf", b"same bytes")
    grown = make_file(tmp_path, "grown.pdf", b"same bytes" + b"\0" * 10)
    assert doc_key(a) == doc_key(b)
    assert doc_key(a) != doc_key(grown)


def test_doc_key_of_unreadable_or_empty_is_none(tmp_path):
    assert doc_key(tmp_path / "nope.pdf") is None
    assert doc_key(make_file(tmp_path, "empty", b"")) is None
    assert doc_key(b"") is None


# Recents


def test_recents_newest_first_without_duplicates(folder):
    s = Store(folder)
    for name in ("a", "b", "c"):
        s.record_open(f"file:///tmp/{name}.pdf", f"{name}.pdf", "pdf")
    s.record_open("file:///tmp/a.pdf", "a.pdf", "pdf")
    assert [r.display_name for r in s.recents()] == ["a.pdf", "c.pdf", "b.pdf"]


def test_recents_keep_fifty_and_report_pushed_out_thumbnails(folder):
    s = Store(folder)
    events = []
    s.add_forget_hook(events.append)
    for i in range(RECENTS_MAX + 5):
        s.record_open(f"file:///tmp/{i}.pdf", f"{i}.pdf", "pdf", thumb_key=f"t{i}")
    names = [r.display_name for r in s.recents()]
    assert len(names) == RECENTS_MAX
    assert names[0] == f"{RECENTS_MAX + 4}.pdf" and names[-1] == "5.pdf"
    pushed = [t for e in events for t in e.thumb_keys]
    assert pushed == ["t0", "t1", "t2", "t3", "t4"]
    assert all(e.doc_keys == () and not e.everything for e in events)


def test_missing_file_is_marked_not_dropped(folder, tmp_path):
    path = make_file(tmp_path, "gone.docx")
    s = Store(folder)
    s.record_open(path.as_uri(), "gone.docx", "word")
    s.record_open("zip-entry://archive/inner.txt", "inner.txt", "text")
    path.unlink()
    by_name = {r.display_name: r for r in Store(folder).recents()}
    assert by_name["gone.docx"].missing is True
    assert by_name["inner.txt"].missing is False


def test_uri_with_escapes_resolves_to_the_file(folder, tmp_path):
    path = make_file(tmp_path, "отчёт 1.docx")
    s = Store(folder)
    s.record_open(path.as_uri(), path.name, "word")
    assert s.recents()[0].missing is False


def test_recents_survive_a_restart(folder):
    Store(folder).record_open("file:///x.pdf", "x.pdf", "pdf", doc_key="k", thumb_key="t")
    (r,) = Store(folder).recents()
    got = (r.uri, r.display_name, r.kind, r.doc_key, r.thumb_key)
    assert got == ("file:///x.pdf", "x.pdf", "pdf", "k", "t")


def test_set_thumb_key(folder):
    s = Store(folder)
    s.record_open("file:///x.pdf", "x.pdf", "pdf")
    s.set_thumb_key("file:///x.pdf", "thumb-1")
    assert Store(folder).recents()[0].thumb_key == "thumb-1"


# Positions


def test_position_round_trip(folder):
    s = Store(folder)
    s.save_position("k1", page=12, offset_y=0.25, zoom=1.5)
    p = Store(folder).position("k1")
    assert (p.page, p.offset_y, p.zoom) == (12, 0.25, 1.5)
    assert Store(folder).position("other") is None


def test_positions_keep_the_500_most_recently_saved(folder):
    s = Store(folder)
    for i in range(POSITIONS_MAX + 3):
        s.save_position(f"k{i}", page=i)
    s.save_position("k1", page=99)  # saving again makes it recent
    reloaded = Store(folder)
    assert reloaded.position("k0") is None
    assert reloaded.position("k1").page == 99
    assert reloaded.position(f"k{POSITIONS_MAX + 2}") is not None
    lines = (folder / store.POSITIONS_FILE).read_text().splitlines()
    assert len(lines) == POSITIONS_MAX


# Remove and clear (11.3)


def test_remove_deletes_entry_and_position_and_tells_hooks(folder, tmp_path):
    path = make_file(tmp_path, "book.epub", b"book bytes")
    key = doc_key(path)
    s = Store(folder)
    events: list[Forgotten] = []
    s.add_forget_hook(events.append)
    s.record_open(path.as_uri(), "book.epub", "epub", doc_key=key, thumb_key="th")
    s.record_open("file:///other.pdf", "other.pdf", "pdf")
    s.save_position(key, page=40)
    s.save_position("unrelated", page=3)

    s.remove(path.as_uri())

    assert [r.display_name for r in Store(folder).recents()] == ["other.pdf"]
    assert Store(folder).position(key) is None
    assert Store(folder).position("unrelated").page == 3
    assert events == [Forgotten((path.as_uri(),), (key,), ("th",), everything=False)]


def test_remove_finds_the_position_of_an_entry_without_a_stored_key(folder, tmp_path):
    path = make_file(tmp_path, "old.pdf", b"old entry")
    s = Store(folder)
    s.record_open(path.as_uri(), "old.pdf", "pdf")
    s.save_position(doc_key(path), page=7)
    s.remove(path.as_uri())
    assert s.position(doc_key(path)) is None


def test_remove_of_unknown_uri_does_nothing(folder):
    s = Store(folder)
    events = []
    s.add_forget_hook(events.append)
    s.remove("file:///never.pdf")
    assert events == []
    assert not (folder / store.RECENTS_FILE).exists()


def test_clear_history_forgets_everything(folder):
    s = Store(folder)
    events = []
    s.add_forget_hook(events.append)
    s.record_open("file:///a.pdf", "a.pdf", "pdf", thumb_key="ta")
    s.save_position("ka", page=5)
    s.clear_history()
    reloaded = Store(folder)
    assert reloaded.recents() == [] and reloaded.position("ka") is None
    (event,) = events
    assert event.everything and event.thumb_keys == ("ta",) and event.doc_keys == ("ka",)


def test_a_failing_hook_does_not_undo_the_removal(folder):
    s = Store(folder)

    def broken(_event):
        raise RuntimeError("cache is gone")

    s.add_forget_hook(broken)
    s.record_open("file:///a.pdf", "a.pdf", "pdf")
    s.remove("file:///a.pdf")
    assert Store(folder).recents() == []


# History off (11.5)


def test_history_off_records_nothing(folder):
    s = Store(folder)
    s.update_settings(history_enabled=False)
    assert s.record_open("file:///a.pdf", "a.pdf", "pdf") is None
    assert s.save_position("k", page=3) is None
    assert s.recents() == [] and s.position("k") is None
    assert not (folder / store.RECENTS_FILE).exists()
    assert not (folder / store.POSITIONS_FILE).exists()


# Damaged files: keep every valid entry, keep a copy of the original


def test_one_bad_line_costs_only_that_entry(folder):
    folder.mkdir()
    good = {"uri": "file:///g.pdf", "display_name": "g.pdf", "kind": "pdf", "opened_at": 5}
    older = {"uri": "file:///o.pdf", "display_name": "o.pdf", "kind": "pdf", "opened_at": 1}
    original = "\n".join(
        [
            json.dumps(good),
            "{not json",
            json.dumps({"uri": "file:///no-name.pdf", "kind": "pdf", "opened_at": 2}),
            json.dumps({**older, "opened_at": "yesterday"}),
            json.dumps([1, 2]),
            json.dumps(older),
        ]
    )
    (folder / store.RECENTS_FILE).write_text(original)

    s = Store(folder)
    assert [r.display_name for r in s.recents()] == ["g.pdf", "o.pdf"]
    assert (folder / "recents.jsonl.bak").read_text() == original

    s.record_open("file:///n.pdf", "n.pdf", "pdf")
    assert [r.display_name for r in Store(folder).recents()] == ["n.pdf", "g.pdf", "o.pdf"]
    assert (folder / "recents.jsonl.bak").read_text() == original


def test_unreadable_file_starts_empty_with_a_backup(folder):
    folder.mkdir()
    (folder / store.POSITIONS_FILE).write_bytes(b"\xff\xfe\x00garbage")
    s = Store(folder)
    assert s.position("anything") is None
    assert (folder / "positions.jsonl.bak").read_bytes() == b"\xff\xfe\x00garbage"
    s.save_position("k", page=2)
    assert Store(folder).position("k").page == 2


def test_clean_file_makes_no_backup(folder):
    s = Store(folder)
    s.record_open("file:///a.pdf", "a.pdf", "pdf")
    Store(folder).recents()
    assert not list(folder.glob("*.bak"))


# Atomic writes


def test_failed_write_leaves_the_old_file_and_no_temp(folder, monkeypatch):
    s = Store(folder)
    s.record_open("file:///a.pdf", "a.pdf", "pdf")
    before = (folder / store.RECENTS_FILE).read_text()

    def no_rename(*_args):
        raise OSError("disk full")

    monkeypatch.setattr(store.os, "replace", no_rename)
    with pytest.raises(OSError):
        s.record_open("file:///b.pdf", "b.pdf", "pdf")
    assert (folder / store.RECENTS_FILE).read_text() == before
    assert sorted(p.name for p in folder.iterdir()) == [store.RECENTS_FILE]


# Settings


def test_settings_defaults():
    assert Settings() == Settings(
        language=None, theme="system", night_mode=False, history_enabled=True, text_page_mb=5
    )


def test_settings_persist(folder):
    Store(folder).update_settings(language="ru", theme="dark", night_mode=True, text_page_mb=8)
    s = Store(folder).settings
    got = (s.language, s.theme, s.night_mode, s.history_enabled, s.text_page_mb)
    assert got == ("ru", "dark", True, True, 8)


@pytest.mark.parametrize(
    "change",
    [
        {"language": "de"},
        {"theme": "blue"},
        {"night_mode": 1},
        {"text_page_mb": 0},
        {"text_page_mb": True},
        {"x": 1},
    ],
)
def test_bad_settings_are_refused(folder, change):
    s = Store(folder)
    with pytest.raises(ValueError):
        s.update_settings(**change)
    assert s.settings == Settings()


def test_settings_keep_good_values_from_a_damaged_file(folder):
    folder.mkdir()
    (folder / store.SETTINGS_FILE).write_text(json.dumps({"language": "ru", "theme": 7, "future": 1}))
    s = Store(folder).settings
    assert s.language == "ru" and s.theme == "system"
    assert (folder / "settings.json.bak").exists()


def test_settings_from_unparseable_file_are_defaults(folder):
    folder.mkdir()
    (folder / store.SETTINGS_FILE).write_text("{")
    assert Store(folder).settings == Settings()
    assert (folder / "settings.json.bak").read_text() == "{"


def test_default_folder_is_the_app_data_folder():
    s = Store()
    s.record_open("file:///a.pdf", "a.pdf", "pdf")
    assert (paths.data_dir() / store.RECENTS_FILE).exists()
