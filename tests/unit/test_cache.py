import hashlib

import pytest

from sovander import paths
from sovander.cache import FULL, PAGE1, RenderCache, content_key, font_set_hash


def key(n: int) -> str:
    return hashlib.sha256(str(n).encode()).hexdigest()


def cache(**kw) -> RenderCache:
    kw.setdefault("font_hash", "fonts-a")
    return RenderCache("LibreOffice 26.2", **kw)


def disk_files() -> list:
    root = paths.cache_dir() / "renders"
    return sorted(p for p in root.rglob("*") if p.is_file()) if root.exists() else []


def test_content_key_is_sha256_of_bytes(tmp_path):
    path = tmp_path / "doc.docx"
    path.write_bytes(b"hello")
    assert content_key(path) == content_key(b"hello") == hashlib.sha256(b"hello").hexdigest()


def test_round_trip_and_miss():
    c = cache()
    assert c.get(key(1), PAGE1) is None
    c.put(key(1), PAGE1, b"%PDF page one")
    c.put(key(1), FULL, b"%PDF all pages")
    assert c.get(key(1), PAGE1) == b"%PDF page one"
    assert c.get(key(1), FULL) == b"%PDF all pages"
    assert all(p.is_relative_to(paths.cache_dir() / "renders") for p in disk_files())


def test_engine_or_font_change_is_a_miss():
    cache().put(key(1), FULL, b"old")
    assert RenderCache("LibreOffice 26.8", font_hash="fonts-a").get(key(1), FULL) is None
    assert RenderCache("LibreOffice 26.2", font_hash="fonts-b").get(key(1), FULL) is None
    assert cache().get(key(1), FULL) == b"old"


def test_purge_removes_every_variant_of_one_document_only():
    cache().put(key(1), FULL, b"a")
    RenderCache("LibreOffice 26.8", font_hash="x").put(key(1), FULL, b"b")
    cache().put(key(2), FULL, b"c")
    cache().purge(key(1))
    assert not (paths.cache_dir() / "renders" / key(1)).exists()
    assert cache().get(key(2), FULL) == b"c"


def test_least_recently_used_document_goes_first():
    c = cache(cap_bytes=2500)
    c.put(key(1), FULL, b"1" * 1000)
    c.put(key(2), FULL, b"2" * 1000)
    assert c.get(key(1), FULL)  # 1 is now newer than 2
    c.put(key(3), FULL, b"3" * 1000)
    assert c.get(key(2), FULL) is None
    assert c.get(key(1), FULL) and c.get(key(3), FULL)
    assert c.size() <= 2500


def test_document_being_shown_stays_even_over_the_cap():
    c = cache(cap_bytes=100)
    c.put(key(1), FULL, b"x" * 500)
    assert c.get(key(1), FULL) == b"x" * 500


def test_write_is_atomic_and_leaves_no_temp_files():
    c = cache()
    c.put(key(1), FULL, b"first")
    c.put(key(1), FULL, b"second")
    assert c.get(key(1), FULL) == b"second"
    assert [p.name for p in disk_files()] == [FULL]


def test_memory_only_writes_nothing_to_disk():
    c = cache(memory_only=True, cap_bytes=2500)
    c.put(key(1), PAGE1, b"1" * 1000)
    c.put(key(2), PAGE1, b"2" * 1000)
    c.get(key(1), PAGE1)
    c.put(key(3), PAGE1, b"3" * 1000)
    assert c.get(key(2), PAGE1) is None and c.get(key(1), PAGE1) and c.get(key(3), PAGE1)
    c.purge(key(1))
    assert c.get(key(1), PAGE1) is None
    assert disk_files() == []


def test_clear_forgets_everything():
    c = cache()
    c.put(key(1), FULL, b"a")
    c.clear()
    assert c.get(key(1), FULL) is None and c.size() == 0


@pytest.mark.parametrize("bad_key, part", [("../../etc", FULL), (key(1).upper(), FULL), (key(1), "../x"),
                                           (key(1), "a/b"), (key(1), "")])
def test_keys_and_parts_cannot_escape_the_cache(bad_key, part):
    with pytest.raises(ValueError):
        cache().put(bad_key, part, b"x")


def test_font_set_hash_follows_font_folders(tmp_path):
    fonts = tmp_path / "fonts"
    fonts.mkdir()
    before = font_set_hash((str(fonts),))
    (fonts / "truetype").mkdir()
    assert font_set_hash((str(fonts),)) != before
    assert font_set_hash((str(tmp_path / "missing"),)) == font_set_hash(())
