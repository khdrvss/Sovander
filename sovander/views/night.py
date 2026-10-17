"""Night mode: light text on a dark page, colours and photos kept (Requirement 12.2).

Lightness is inverted while hue and saturation stay: in HSL, L becomes 1 - L. For a pixel this works out
to c' = c + 255 - max(r, g, b) - min(r, g, b), so white paper turns black, black text turns white, grey
inverts and a red heading stays red. Rectangles holding images (``page.get_image_info()``) are copied
back unchanged, so photographs look as printed.

Why not ``fitz.Pixmap.invert_irect``: it is plain RGB inversion (c' = 255 - c), which turns red into cyan
and blue into orange. Rows without colour get exactly that inversion here, at the same C speed, through
``bytes.translate``; only rows holding colour pay for the per-pixel formula.

Speed for one full page at 150 dpi (1240 x 1755), measured 2026-10-17: a text page about 15 ms in pure
Python; a slide with a coloured background about 0.3 s in pure Python and 0.02 s when numpy is
installed, which is used only if present (it is not a dependency).
"""

from operator import add, sub

try:
    import pymupdf as fitz
except ImportError:  # PyMuPDF before 1.24 (Ubuntu 22.04 and 24.04) has only the old name
    import fitz

try:
    import numpy
except ImportError:  # optional: the pure-Python path gives the same bytes
    numpy = None

_INVERT = bytes(range(255, -1, -1))


def invert_lightness(
    samples: bytes, width: int, height: int, n: int = 3, keep=(), use_numpy: bool | None = None
) -> bytes:
    """Return a night-mode copy of a packed RGB (n=3) or RGBA (n=4) buffer.

    ``keep`` holds (x0, y0, x1, y1) pixel rectangles copied unchanged from the input. Alpha is kept.
    ``use_numpy`` forces a path (tests); by default numpy is used when it is importable.
    """
    if n not in (3, 4):
        raise ValueError(f"expected 3 or 4 channels, got {n}")
    if len(samples) != width * height * n:
        raise ValueError("buffer size does not match width x height x channels")
    if use_numpy is None:
        use_numpy = numpy is not None
    out = _with_numpy(samples, n) if use_numpy else _pure(samples, width, height, n)
    _restore(out, samples, width, height, n, keep)
    return bytes(out)


def _pure(samples: bytes, width: int, height: int, n: int) -> bytearray:
    out = bytearray(samples.translate(_INVERT))
    if n == 4:
        out[3::4] = samples[3::4]
    stride = width * n
    done: dict[bytes, bytes | None] = {}  # identical rows (backgrounds, margins) are worked out once
    for top in range(0, height * stride, stride):
        row = bytes(out[top : top + stride])
        if row not in done:
            done[row] = _hue_back(row, n)
        fixed = done[row]
        if fixed is not None:
            out[top : top + stride] = fixed
    return out


def _hue_back(inverted: bytes, n: int) -> bytes | None:
    """On an RGB-inverted row, c' = max + min - c turns the hue back; None when the row is grey."""
    r, g, b = inverted[0::n], inverted[1::n], inverted[2::n]
    if r == g == b:
        return None
    total = list(map(add, map(max, r, g, b), map(min, r, g, b)))
    row = bytearray(inverted)
    row[0::n] = bytes(map(sub, total, r))
    row[1::n] = bytes(map(sub, total, g))
    row[2::n] = bytes(map(sub, total, b))
    return bytes(row)


def _with_numpy(samples: bytes, n: int) -> bytearray:
    px = numpy.frombuffer(samples, numpy.uint8).reshape(-1, n)
    r, g, b = px[:, 0], px[:, 1], px[:, 2]
    shift = (
        255
        - numpy.maximum(numpy.maximum(r, g), b).astype(numpy.int16)
        - numpy.minimum(numpy.minimum(r, g), b)
    )
    out = px.copy()
    out[:, 0] = r + shift
    out[:, 1] = g + shift
    out[:, 2] = b + shift
    return bytearray(out.tobytes())


def _restore(out: bytearray, samples: bytes, width: int, height: int, n: int, keep) -> None:
    stride = width * n
    for x0, y0, x1, y1 in keep:
        x0, x1 = max(0, int(x0)), min(width, int(x1))
        y0, y1 = max(0, int(y0)), min(height, int(y1))
        if x0 >= x1:
            continue
        for y in range(y0, y1):
            start, end = y * stride + x0 * n, y * stride + x1 * n
            out[start:end] = samples[start:end]


def image_boxes(page, matrix, origin=(0, 0)) -> list[tuple[int, int, int, int]]:
    """Pixel rectangles of the page's images in a pixmap drawn with ``matrix``.

    ``origin`` is the pixmap's (x, y) when it was drawn with a clip. Caller holds the fitz lock.
    """
    boxes = []
    ox, oy = origin
    for info in page.get_image_info():
        rect = fitz.Rect(info["bbox"]) * matrix
        if rect.is_empty or rect.is_infinite:
            continue
        # Round outwards so anti-aliased photo edges are kept too
        boxes.append((int(rect.x0) - ox, int(rect.y0) - oy, int(rect.x1 + 1) - ox, int(rect.y1 + 1) - oy))
    return boxes
