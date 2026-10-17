"""Thumbnails: drawn off the main thread, kept only in the cache folder, forgotten with their entry (11)."""

import time
from pathlib import Path

import pytest

gi = pytest.importorskip("gi")
gi.require_version("GdkPixbuf", "2.0")
from gi.repository import GdkPixbuf, GLib  # noqa: E402

from sovander import paths, thumbs  # noqa: E402
from sovander.cache import PAGE1, RenderCache, content_key  # noqa: E402
from sovander.store import Store, doc_key  # noqa: E402
from sovander.thumbs import THUMB_PX, Thumbnails, image_png, pdf_png, thumb_key  # noqa: E402

try:
    import pymupdf as fitz
except ImportError:
    import fitz

PNG_MAGIC = b"\x89PNG\r\n\x1a\n"


def make_pdf(path: Path | None = None, size=(595, 842)) -> bytes:
    doc = fitz.open()
    page = doc.new_page(width=size[0], height=size[1])
    page.draw_rect(fitz.Rect(50, 50, 200, 200), color=None, fill=(0.8, 0.1, 0.1))
    data = doc.tobytes()
    if path is not None:
        path.write_bytes(data)
    return data


def big_png(width=1200, height=800) -> bytes:
    pixbuf = GdkPixbuf.Pixbuf.new(GdkPixbuf.Colorspace.RGB, False, 8, width, height)
    pixbuf.fill(0x3366CCFF)
    return bytes(pixbuf.save_to_bufferv("png", [], [])[1])


def png_size(png: bytes) -> tuple[int, int]:
    loader = GdkPixbuf.PixbufLoader()
    loader.write(png)
    loader.close()
    pixbuf = loader.get_pixbuf()
    return pixbuf.get_width(), pixbuf.get_height()


def pump(until, timeout=10.0) -> bool:
    context = GLib.MainContext.default()
    end = time.monotonic() + timeout
    while not until():
        if time.monotonic() > end:
            return False
        if not context.iteration(False):
            time.sleep(0.005)
    return True


@pytest.fixture
def store(tmp_path):
    return Store(tmp_path / "data")


@pytest.fixture
def files(tmp_path):
    folder = tmp_path / "docs"
    folder.mkdir()
    return folder


def recent_of(store, uri):
    return next(r for r in store.recents() if r.uri == uri)


def draw(store, thumbnails, path: Path, kind: str):
    """Record an open, ask for its thumbnail and wait; returns (recent, png path or None)."""
    uri = str(path)
    store.record_open(uri, path.name, kind, doc_key=doc_key(path))
    got = []
    assert thumbnails.request(recent_of(store, uri), lambda u, png: got.append((u, png)))
    assert pump(lambda: got)
    return recent_of(store, uri), got[0][1]


# Drawing


def test_pdf_page_one_scaled_to_the_thumbnail_size():
    png = pdf_png(make_pdf(size=(842, 595)))
    assert png.startswith(PNG_MAGIC)
    assert png_size(png) == (THUMB_PX, round(595 * THUMB_PX / 842))


def test_locked_damaged_or_empty_pdfs_get_no_thumbnail(fixture_file):
    assert pdf_png(fixture_file("encrypted.pdf")) is None
    assert pdf_png(b"%PDF-1.7 not really") is None
    assert pdf_png(b"") is None


def test_images_are_scaled_down_and_never_up(fixture_file):
    assert png_size(image_png(big_png(1200, 800))) == (THUMB_PX, round(800 * THUMB_PX / 1200))
    tiny = fixture_file("tiny.png").read_bytes()
    original = png_size(tiny)
    assert png_size(image_png(tiny)) == original


def test_an_svg_is_drawn_from_bytes(fixture_file):
    assert image_png(fixture_file("icon.svg").read_bytes()).startswith(PNG_MAGIC)


def test_an_image_too_large_to_decode_is_refused_before_its_pixels():
    import struct
    import zlib

    def chunk(kind, body):
        return struct.pack(">I", len(body)) + kind + body + struct.pack(">I", zlib.crc32(kind + body))

    header = struct.pack(">IIBBBBB", 30000, 30000, 8, 2, 0, 0, 0)
    bomb = PNG_MAGIC + chunk(b"IHDR", header) + chunk(b"IDAT", zlib.compress(b"\0" * 1000))
    assert image_png(bomb) is None


def test_bytes_that_are_no_image_get_none():
    assert image_png(b"not an image at all") is None
    assert image_png(b"") is None


def test_kinds_without_a_thumbnail_have_an_icon():
    assert thumbs.icon_name("sheet") == "x-office-spreadsheet-symbolic"
    assert thumbs.icon_name("zip") == "package-x-generic-symbolic"
    assert thumbs.icon_name("something new") == "text-x-generic-symbolic"


def test_keys_are_file_names_that_change_with_the_content():
    key = thumb_key("/a.pdf", "1" * 32)
    assert len(key) == 32 and int(key, 16) >= 0
    assert key == thumb_key("/a.pdf", "1" * 32)
    assert key != thumb_key("/a.pdf", "2" * 32) != thumb_key("/b.pdf", "1" * 32)


# The worker and the store


def test_a_pdf_thumbnail_is_written_to_the_cache_and_recorded(store, files):
    thumbnails = Thumbnails(store)
    recent, png = draw(store, thumbnails, make_pdf_file(files / "a.pdf"), "pdf")
    assert png is not None and png.read_bytes().startswith(PNG_MAGIC)
    assert png.parent == paths.cache_dir() / "thumbs"  # nowhere but Sovander's own folders (9.10)
    assert recent.thumb_key == png.stem
    assert thumbnails.path(recent) == png


def make_pdf_file(path: Path) -> Path:
    make_pdf(path)
    return path


def test_an_image_thumbnail(store, files):
    image = files / "photo.png"
    image.write_bytes(big_png())
    _recent, png = draw(store, Thumbnails(store), image, "image")
    assert max(png_size(png.read_bytes())) == THUMB_PX


def test_office_files_take_page_one_of_the_cached_render(store, files):
    document = files / "report.docx"
    document.write_bytes(b"PK\x03\x04 pretend this is Word")
    cache = RenderCache("engine-1", font_hash="f")
    cache.put(content_key(document), PAGE1, make_pdf())
    thumbnails = Thumbnails(store, render_cache=cache)
    _recent, png = draw(store, thumbnails, document, "word")
    assert png is not None


def test_office_files_without_a_render_and_other_kinds_ask_for_nothing(store, files):
    thumbnails = Thumbnails(store)
    for name, kind in (("report.docx", "word"), ("budget.xlsx", "sheet"), ("notes.md", "markdown")):
        path = files / name
        path.write_bytes(b"x")
        store.record_open(str(path), name, kind)
        assert not thumbnails.request(recent_of(store, str(path)))


def test_nothing_is_drawn_while_history_is_off(store, files):
    path = make_pdf_file(files / "a.pdf")
    store.record_open(str(path), "a.pdf", "pdf")
    recent = recent_of(store, str(path))
    store.update_settings(history_enabled=False)
    thumbnails = Thumbnails(store)
    assert not thumbnails.request(recent)
    thumbnails.remember(str(path), "pdf", path=path)
    assert thumbnails.join()
    assert not (paths.cache_dir() / "thumbs").exists() or not list((paths.cache_dir() / "thumbs").iterdir())


def test_a_missing_file_asks_for_nothing(store, files):
    path = make_pdf_file(files / "a.pdf")
    store.record_open(str(path), "a.pdf", "pdf")
    path.unlink()
    assert not Thumbnails(store).request(recent_of(store, str(path)))


def test_one_file_is_tried_once_a_session(store, files):
    path = files / "broken.pdf"
    path.write_bytes(b"%PDF-1.4 broken")
    store.record_open(str(path), "broken.pdf", "pdf")
    thumbnails = Thumbnails(store)
    got = []
    assert thumbnails.request(recent_of(store, str(path)), lambda u, png: got.append(png))
    assert pump(lambda: got) and got == [None]
    assert not thumbnails.request(recent_of(store, str(path)))


def test_remember_draws_from_the_engine_pdf_given(store, files):
    document = files / "deck.pptx"
    document.write_bytes(b"PK\x03\x04 a deck")
    store.record_open(str(document), "deck.pptx", "slides")
    thumbnails = Thumbnails(store)
    got = []
    thumbnails.remember(str(document), "slides", pdf=make_pdf(), done=lambda u, png: got.append(png))
    assert pump(lambda: got) and got[0] is not None
    assert recent_of(store, str(document)).thumb_key == got[0].stem


# Forgetting (11.3)


def test_removing_an_entry_deletes_its_thumbnail(store, files):
    thumbnails = Thumbnails(store)
    recent, png = draw(store, thumbnails, make_pdf_file(files / "a.pdf"), "pdf")
    store.remove(recent.uri)
    assert not png.exists()


def test_clearing_history_deletes_every_thumbnail(store, files):
    thumbnails = Thumbnails(store)
    pngs = [draw(store, thumbnails, make_pdf_file(files / f"{n}.pdf"), "pdf")[1] for n in "abc"]
    store.clear_history()
    assert not any(png.exists() for png in pngs)


def test_an_entry_pushed_off_the_list_takes_its_thumbnail(store, files, monkeypatch):
    from sovander import store as store_module

    monkeypatch.setattr(store_module, "RECENTS_MAX", 2)
    thumbnails = Thumbnails(store)
    _first, png = draw(store, thumbnails, make_pdf_file(files / "a.pdf"), "pdf")
    for name in ("b.pdf", "c.pdf"):
        store.record_open(str(files / name), name, "pdf")
    assert not png.exists()


def test_a_thumbnail_finished_after_its_entry_went_is_not_kept(store, files):
    path = make_pdf_file(files / "a.pdf")
    store.record_open(str(path), "a.pdf", "pdf")
    thumbnails = Thumbnails(store)
    before = thumbnails._forgets
    store.remove(str(path))  # while the worker was drawing it
    thumbnails._draw(before, str(path), "pdf", path, None, None, None)
    assert list((paths.cache_dir() / "thumbs").glob("*.png")) == []


def test_prune_deletes_thumbnails_no_entry_names(store, files):
    thumbnails = Thumbnails(store)
    _recent, kept = draw(store, thumbnails, make_pdf_file(files / "a.pdf"), "pdf")
    orphan = kept.with_name("0" * 32 + ".png")
    orphan.write_bytes(kept.read_bytes())
    thumbnails.prune()
    assert thumbnails.join()
    assert kept.exists() and not orphan.exists()


def test_one_thumbnails_per_store_with_one_hook(store):
    first = thumbs.for_store(store)
    assert thumbs.for_store(store) is first
    assert len(store._forget_hooks) == 1


def test_a_bad_key_never_reaches_the_file_system(store, tmp_path):
    victim = tmp_path / "victim.png"
    victim.write_bytes(b"keep me")
    thumbnails = Thumbnails(store, folder=tmp_path / "thumbs")
    thumbnails._on_forget(thumbs.Forgotten(uris=(), doc_keys=(), thumb_keys=("../victim",)))
    assert victim.exists()


def test_a_pdf_from_the_engine_gets_a_thumbnail():
    """The engine hands over PdfChunk (a bytes subclass); Ubuntu's PyMuPDF (< 1.24) refuses anything but
    bytes, which left every office file without a thumbnail when only newer PyMuPDF was tested."""
    from sovander.engine.client import PdfChunk

    png = pdf_png(PdfChunk.of(make_pdf(), final=True, damaged=False))
    assert png is not None and max(png_size(png)) == THUMB_PX
