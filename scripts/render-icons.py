"""Pictures made from the app icon: docs/icon.png (README) and docs/social-preview.png (1280×640, for
GitHub's Settings → Social preview). Uses GdkPixbuf, Pango and cairo, which the app already needs.
    python3 scripts/render-icons.py
"""

from pathlib import Path

import cairo
import gi

gi.require_version("Gdk", "4.0")
gi.require_version("GdkPixbuf", "2.0")
gi.require_version("Pango", "1.0")
gi.require_version("PangoCairo", "1.0")
from gi.repository import Gdk, GdkPixbuf, Pango, PangoCairo  # noqa: E402

REPO = Path(__file__).resolve().parents[1]
ICON = REPO / "data/icons/hicolor/scalable/apps/io.github.khdrvss.Sovander.svg"
DOCS = REPO / "docs"


def icon(size: int) -> GdkPixbuf.Pixbuf:
    return GdkPixbuf.Pixbuf.new_from_file_at_size(str(ICON), size, size)


def text(ctx, words: str, font: str, x: float, y: float, rgb: tuple[float, float, float]) -> None:
    layout = PangoCairo.create_layout(ctx)
    layout.set_font_description(Pango.FontDescription.from_string(font))
    layout.set_text(words, -1)
    ctx.set_source_rgb(*rgb)
    ctx.move_to(x, y)
    PangoCairo.show_layout(ctx, layout)


def social_preview(path: Path) -> None:
    surface = cairo.ImageSurface(cairo.FORMAT_RGB24, 1280, 640)
    ctx = cairo.Context(surface)
    ctx.set_source_rgb(0x1A / 255, 0x5F / 255, 0xB4 / 255)  # GNOME blue 5, the icon's own
    ctx.paint()
    ctx.set_source_rgb(0xF6 / 255, 0xD3 / 255, 0x2D / 255)  # the eyes' amber, as a band
    ctx.rectangle(0, 600, 1280, 40)
    ctx.fill()
    Gdk.cairo_set_source_pixbuf(ctx, icon(360), 100, 120)
    ctx.paint()
    text(ctx, "Sovander", "Sans Bold 96", 520, 170, (1, 1, 1))
    text(ctx, "Office files, PDFs and books.\nOffline. View-only. Accurate.", "Sans 36", 524, 330,
         (0xDE / 255, 0xEC / 255, 0xFB / 255))
    surface.write_to_png(str(path))


def main() -> None:
    DOCS.mkdir(exist_ok=True)
    icon(256).savev(str(DOCS / "icon.png"), "png", [], [])
    social_preview(DOCS / "social-preview.png")
    print(DOCS / "icon.png", DOCS / "social-preview.png", sep="\n")


if __name__ == "__main__":
    main()
