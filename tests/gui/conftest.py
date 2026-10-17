"""GUI test helpers: a display check, a main-loop pump, PDFs built on the fly and pixel capture.

Run under ``xvfb-run -a``; without a display every test here is skipped.
"""

import time

import pytest

gi = pytest.importorskip("gi")
gi.require_version("Gdk", "4.0")
gi.require_version("Graphene", "1.0")
gi.require_version("Gtk", "4.0")
gi.require_version("Adw", "1")
from gi.repository import Adw, Gdk, GLib, Graphene, Gtk  # noqa: E402

try:
    import pymupdf as fitz
except ImportError:
    import fitz

RED = (0.8, 0.1, 0.1)
RED_BOX = (72, 150, 272, 250)  # points; every test page has it


@pytest.fixture(scope="session", autouse=True)
def display():
    if Gdk.Display.get_default() is None:
        pytest.skip("no display: run under xvfb-run -a")
    # The app runs under libadwaita, which follows the desktop's light/dark preference and then ignores
    # GtkSettings; start it here so every test sees the same, light, theme whatever this machine prefers
    Adw.init()


@pytest.fixture(autouse=True)
def light_theme(display):
    """Each test starts light and leaves light, so a test that goes dark cannot colour the next one."""
    styles = Adw.StyleManager.get_default()
    styles.set_color_scheme(Adw.ColorScheme.FORCE_LIGHT)
    yield styles
    styles.set_color_scheme(Adw.ColorScheme.FORCE_LIGHT)


def pump(until, timeout=10.0) -> bool:
    """Run the GLib main loop until ``until()`` is true or ``timeout`` seconds pass."""
    context = GLib.MainContext.default()
    end = time.monotonic() + timeout
    while not until():
        if time.monotonic() > end:
            return False
        if not context.iteration(False):
            time.sleep(0.002)
    return True


@pytest.fixture
def wait():
    return pump


def build_pdf(pages=30, size=(595, 842), photo=False) -> bytes:
    """Numbered pages with a red box; ``photo`` adds a dark-blue heading bar and a small image."""
    doc = fitz.open()
    for i in range(pages):
        page = doc.new_page(width=size[0], height=size[1])
        page.insert_text((72, 100), f"Page {i + 1}", fontsize=36)
        page.draw_rect(fitz.Rect(RED_BOX), color=None, fill=RED)
        if photo:
            page.draw_rect(fitz.Rect(72, 300, 472, 340), color=None, fill=(0, 0, 0.5))
            picture = fitz.Pixmap(fitz.csRGB, fitz.IRect(0, 0, 8, 8), False)
            picture.set_rect(picture.irect, (40, 160, 60))
            page.insert_image(fitz.Rect(72, 400, 272, 550), pixmap=picture)
    return doc.tobytes()


@pytest.fixture
def make_pdf():
    return build_pdf


@pytest.fixture
def pixels():
    """The widget as it would be drawn now, as an RGBA fitz.Pixmap."""

    def capture(widget):
        if gi.version_info < (3, 44):
            # PyGObject 3.42 (Ubuntu 22.04) cannot hand a GskRenderNode back to GTK ("expected GObject but
            # got Gsk.ColorNode") and then crashes freeing it; skip before making one. The app never does.
            pytest.skip("pixel checks need PyGObject 3.44 or newer (Ubuntu 24.04)")
        w, h = widget.get_width(), widget.get_height()
        snapshot = Gtk.Snapshot()
        widget.do_snapshot(snapshot)
        texture = (
            widget.get_native()
            .get_renderer()
            .render_texture(snapshot.to_node(), Graphene.Rect().init(0, 0, w, h))
        )
        return fitz.Pixmap(texture.save_to_png_bytes().get_data())

    return capture


@pytest.fixture
def window():
    win = Gtk.Window(default_width=800, default_height=600)
    yield win
    win.destroy()
    pump(lambda: False, 0.02)


@pytest.fixture
def viewer(window):
    from sovander.views.page_view import PageView

    view = PageView()
    window.set_child(Gtk.ScrolledWindow(child=view))
    window.present()
    assert pump(lambda: view.get_width() > 0 and view.get_height() > 0)
    return view
