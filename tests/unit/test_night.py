"""Night mode keeps hue, inverts lightness and leaves photos alone (Requirement 12.2)."""

import colorsys
import os

import pytest

from sovander.views import night

try:
    import pymupdf as fitz
except ImportError:
    import fitz

PATHS = [False] + ([True] if night.numpy is not None else [])


def one(rgb, use_numpy):
    return tuple(night.invert_lightness(bytes(rgb), 1, 1, 3, use_numpy=use_numpy))


@pytest.mark.parametrize("use_numpy", PATHS)
def test_paper_turns_black_and_text_white(use_numpy):
    assert one((255, 255, 255), use_numpy) == (0, 0, 0)
    assert one((0, 0, 0), use_numpy) == (255, 255, 255)
    assert one((200, 200, 200), use_numpy) == (55, 55, 55)


@pytest.mark.parametrize("use_numpy", PATHS)
def test_hue_and_saturation_are_kept(use_numpy):
    for rgb in [(255, 0, 0), (0, 0, 128), (30, 120, 60), (250, 180, 20), (90, 10, 140)]:
        out = one(rgb, use_numpy)
        h1, l1, s1 = colorsys.rgb_to_hls(*(c / 255 for c in rgb))
        h2, l2, s2 = colorsys.rgb_to_hls(*(c / 255 for c in out))
        assert h2 == pytest.approx(h1, abs=0.01), rgb
        assert s2 == pytest.approx(s1, abs=0.02), rgb
        assert l2 == pytest.approx(1 - l1, abs=0.01), rgb


def test_dark_blue_heading_becomes_light_blue():
    assert one((0, 0, 128), False) == (127, 127, 255)


@pytest.mark.parametrize("use_numpy", PATHS)
def test_kept_rectangles_and_alpha_are_untouched(use_numpy):
    w, h = 6, 4
    src = bytes(c for _ in range(w * h) for c in (255, 255, 255, 77))
    out = night.invert_lightness(src, w, h, 4, keep=[(1, 1, 3, 3)], use_numpy=use_numpy)
    px = [tuple(out[i : i + 4]) for i in range(0, len(out), 4)]
    for y in range(h):
        for x in range(w):
            inside = 1 <= x < 3 and 1 <= y < 3
            assert px[y * w + x] == ((255, 255, 255, 77) if inside else (0, 0, 0, 77)), (x, y)


def test_keep_rectangles_are_clipped_to_the_buffer():
    src = bytes([255] * 12)
    out = night.invert_lightness(src, 2, 2, 3, keep=[(-5, -5, 1, 1), (10, 10, 20, 20)], use_numpy=False)
    assert out[:3] == bytes([255] * 3) and out[3:] == bytes(9)


@pytest.mark.skipif(night.numpy is None, reason="numpy not installed")
def test_numpy_and_pure_python_agree_on_noise():
    w, h = 64, 48
    src = os.urandom(w * h * 3)
    keep = [(5, 5, 20, 30)]
    assert night.invert_lightness(src, w, h, 3, keep, use_numpy=True) == night.invert_lightness(
        src, w, h, 3, keep, use_numpy=False
    )


def test_bad_buffers_are_refused():
    with pytest.raises(ValueError):
        night.invert_lightness(b"\0" * 5, 1, 1, 3)
    with pytest.raises(ValueError):
        night.invert_lightness(b"\0" * 2, 1, 1, 2)


def test_image_boxes_follow_the_page_images():
    doc = fitz.open()
    page = doc.new_page(width=200, height=200)
    photo = fitz.Pixmap(fitz.csRGB, fitz.IRect(0, 0, 4, 4), False)
    photo.set_rect(photo.irect, (200, 30, 30))
    page.insert_image(fitz.Rect(50, 60, 150, 110), pixmap=photo)
    page.insert_text((20, 30), "Title", color=(0, 0, 0))

    matrix = fitz.Matrix(2, 2)
    boxes = night.image_boxes(page, matrix)
    assert len(boxes) == 1
    x0, y0, x1, y1 = boxes[0]
    assert (x0, y0) == (100, 120) and 300 <= x1 <= 301 and 220 <= y1 <= 221

    pix = page.get_pixmap(matrix=matrix, alpha=False)
    out = night.invert_lightness(pix.samples, pix.width, pix.height, pix.n, boxes)
    at = (130 * pix.width + 200) * 3  # inside the photo
    assert tuple(out[at : at + 3]) == tuple(pix.samples[at : at + 3]) == (200, 30, 30)
    assert tuple(out[0:3]) == (0, 0, 0)  # paper corner is now dark

    # A clipped pixmap shifts the boxes by its origin
    assert night.image_boxes(page, matrix, origin=(100, 100)) == [(0, 20, x1 - 100, y1 - 100)]
