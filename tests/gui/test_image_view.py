import gc
import resource
import struct
import time
import weakref
import zlib

import pytest

gi = pytest.importorskip("gi")
gi.require_version("Gtk", "4.0")
gi.require_version("GdkPixbuf", "2.0")
from gi.repository import Gdk, GdkPixbuf, GLib, Gtk  # noqa: E402

from sovander import i18n  # noqa: E402
from sovander.views import image_view  # noqa: E402
from sovander.views.image_view import (  # noqa: E402
    ZOOM_MAX,
    ZOOM_MIN,
    ZOOM_STEP,
    DamagedImage,
    ImageTooLarge,
    ImageView,
    UnsupportedImage,
    anchored_scroll,
    decode,
)

pytestmark = pytest.mark.gui

FORMATS = {f.get_name() for f in GdkPixbuf.Pixbuf.get_formats()}


@pytest.fixture(autouse=True)
def english():
    i18n.set_language("en")
    yield
    i18n.set_language("en")


@pytest.fixture
def shown():
    """Put a view in a window of a given size; windows are closed after the test."""
    windows = []

    def make(width=400, height=300):
        view = ImageView()
        window = Gtk.Window(default_width=width, default_height=height, child=view)
        window.present()
        windows.append(window)
        spin(0.1)
        return view

    yield make
    for window in windows:
        window.destroy()


def spin(seconds):
    context = GLib.MainContext.default()
    end = time.monotonic() + seconds
    while time.monotonic() < end:
        context.iteration(False)
        time.sleep(0.005)


def png(width, height, rows=None):
    """A PNG built by hand: grey RGB rows, or for ``rows=None`` a header and a scrap of data.

    libpng reports the size only on reaching image data, so even a hostile file needs some.
    """

    def chunk(kind, data):
        return struct.pack(">I", len(data)) + kind + data + struct.pack(">I", zlib.crc32(kind + data))

    body = chunk(b"IHDR", struct.pack(">IIBBBBB", width, height, 8, 2, 0, 0, 0))
    if rows is None:
        return b"\x89PNG\r\n\x1a\n" + body + chunk(b"IDAT", zlib.compress(b"\0" * 4096))
    raw = b"".join(b"\0" + bytes([rows]) * (width * 3) for _ in range(height))
    return b"\x89PNG\r\n\x1a\n" + body + chunk(b"IDAT", zlib.compress(raw)) + chunk(b"IEND", b"")


def pixels(pixbuf):
    return pixbuf.get_width(), pixbuf.get_height(), pixbuf.read_pixel_bytes().get_data()


# Decoding


def test_still_image(fixture_file):
    d = decode(fixture_file("tiny.png").read_bytes())
    assert (d.pixbuf.get_width(), d.pixbuf.get_height()) == (64, 64)
    assert d.animation is None and d.format_name == "png"


@pytest.mark.parametrize(
    ("n", "rotation"),
    [
        (1, None),
        (3, GdkPixbuf.PixbufRotation.UPSIDEDOWN),
        (6, GdkPixbuf.PixbufRotation.CLOCKWISE),
        (8, GdkPixbuf.PixbufRotation.COUNTERCLOCKWISE),
    ],
)
def test_exif_orientation_is_applied(fixture_file, n, rotation):
    # Every exif-N.jpg holds the same 120x80 pixels; only the orientation tag differs
    raw = GdkPixbuf.Pixbuf.new_from_file(str(fixture_file("exif-1.jpg")))
    expected = raw if rotation is None else raw.rotate_simple(rotation)
    assert pixels(decode(fixture_file(f"exif-{n}.jpg").read_bytes()).pixbuf) == pixels(expected)


@pytest.mark.parametrize("n", [5, 7])
def test_transposed_orientations_turn_the_image(fixture_file, n):
    d = decode(fixture_file(f"exif-{n}.jpg").read_bytes())
    assert (d.pixbuf.get_width(), d.pixbuf.get_height()) == (80, 120)


@pytest.mark.parametrize("name", ["anim.gif", "moving.webp"])
def test_animations_are_kept(fixture_file, name):
    assert decode(fixture_file(name).read_bytes()).animation is not None


def test_apng_shows_its_first_frame(fixture_file):
    # gdk-pixbuf alone rejects it: shared-mime-info calls it image/apng, which no loader claims
    d = decode(fixture_file("moving.png").read_bytes())
    assert (d.pixbuf.get_width(), d.pixbuf.get_height()) == (48, 48)


def test_svg_and_bmp(fixture_file):
    assert decode(fixture_file("icon.svg").read_bytes()).pixbuf.get_width() == 120
    assert decode(fixture_file("photo.bmp").read_bytes()).pixbuf.get_width() == 160


def test_heic_opens_when_its_loader_is_installed(fixture_file):
    data = fixture_file("photo.heic").read_bytes()
    if "heif/avif" in FORMATS or "heif" in FORMATS:
        d = decode(data)
        assert (d.pixbuf.get_width(), d.pixbuf.get_height()) == (160, 96)
    else:
        with pytest.raises(UnsupportedImage, match="HEIC"):
            decode(data)


def test_heic_finds_its_loader_by_brand(fixture_file):
    """shared-mime-info 2.1 (Ubuntu 22.04) types HEIF by file name only; bytes reach the loader by brand."""
    data = fixture_file("photo.heic").read_bytes()
    name = image_view._loader_by_magic(data)
    if "heif/avif" in FORMATS or "heif" in FORMATS:
        assert image_view._decode(data, name).pixbuf.get_width() == 160
    else:
        assert name is None


def test_missing_loader_names_the_format():
    assert str(UnsupportedImage("AVIF")) == "No decoder for AVIF images is installed"
    assert image_view._format_label(b"\0\0\0\x1cftypavif" + b"\0" * 20) == "AVIF"
    assert image_view._format_label(b"\0\0\0\x18ftypheic" + b"\0" * 20) == "HEIC"


def test_text_is_not_an_image(fixture_file):
    with pytest.raises(UnsupportedImage) as caught:
        decode(fixture_file("plain.txt").read_bytes())
    assert str(caught.value) == "This image format is not supported"


def test_empty_and_truncated_images_are_damaged(fixture_file):
    with pytest.raises(DamagedImage):
        decode(b"")
    with pytest.raises(DamagedImage):
        decode(fixture_file("tiny.png").read_bytes()[:40])


@pytest.mark.parametrize("side", [20_000, 100_000])
def test_huge_image_is_refused_before_its_pixels_are_allocated(side):
    peak = resource.getrusage(resource.RUSAGE_SELF).ru_maxrss
    with pytest.raises(ImageTooLarge):
        decode(png(side, side))
    # 20,000 squared is 1.2 GB of pixels; the peak must not move by anything like that
    assert resource.getrusage(resource.RUSAGE_SELF).ru_maxrss - peak < 200 * 1024


def test_very_wide_image_is_scaled_to_the_texture_limit():
    d = decode(png(17_000, 10, rows=128))
    assert d.pixbuf.get_width() == image_view.MAX_SIDE


def test_error_messages_follow_the_language():
    i18n.set_language("ru")
    assert str(DamagedImage()) == "Изображение повреждено"
    assert str(UnsupportedImage("HEIC")) == "Не установлен декодер изображений HEIC"


# Zoom


def test_anchored_scroll_keeps_the_point_under_the_pointer():
    # A point 50 px into a 1000 px image stays 50 px from the edge after doubling
    assert anchored_scroll(0, 50, 1.0, 2.0, 1000, 200) == 50
    # A centred small image stays centred and unscrolled
    assert anchored_scroll(0, 200, 1.0, 2.0, 100, 400) == 0
    # Never past the end
    assert anchored_scroll(800, 199, 1.0, 1.0, 1000, 200) == 800
    assert anchored_scroll(800, 199, 1.0, 0.5, 1000, 200) == 300


def test_large_image_starts_fitted(fixture_file, shown):
    view = shown(100, 60)
    view.load_path(fixture_file("photo.webp"))  # 160 x 96
    spin(0.2)
    width, height = view.scroller.get_width(), view.scroller.get_height()
    assert view.fit
    assert view.zoom == pytest.approx(min(width / 160, height / 96))
    assert view.zoom < 1


def test_small_image_is_not_enlarged_to_fit(fixture_file, shown):
    view = shown(400, 300)
    view.load_path(fixture_file("tiny.png"))
    spin(0.1)
    assert view.fit and view.zoom == 1.0


def test_zoom_steps_and_limits(fixture_file, shown):
    view = shown()
    view.load_path(fixture_file("tiny.png"))
    spin(0.1)
    view.zoom_in()
    view.zoom_in()  # straight after: must build on the first step, not on a stale value
    assert view.zoom == pytest.approx(ZOOM_STEP**2) and not view.fit
    view.zoom_out()
    assert view.zoom == pytest.approx(ZOOM_STEP)
    view.set_zoom(1000)
    assert view.zoom == ZOOM_MAX
    view.set_zoom(0)
    assert view.zoom == ZOOM_MIN
    view.fit_to_window()
    spin(0.1)
    assert view.fit and view.zoom == 1.0


def test_zoomed_image_scrolls_around_the_anchor(shown):
    view = shown(200, 200)
    view.load_bytes(png(1000, 1000, rows=200))
    view.set_zoom(1.0)
    spin(0.2)
    hadj = view.scroller.get_hadjustment()
    hadj.set_value(100)
    view.set_zoom(2.0, anchor=(50, 50))
    spin(0.2)
    assert hadj.get_upper() == 2000
    assert hadj.get_value() == pytest.approx((100 + 50) * 2 - 50)


class FakeScroll:
    def __init__(self, ctrl):
        self.state = Gdk.ModifierType.CONTROL_MASK if ctrl else Gdk.ModifierType(0)

    def get_current_event_state(self):
        return self.state


def test_ctrl_scroll_zooms_and_plain_scroll_scrolls(fixture_file, shown):
    view = shown()
    view.load_path(fixture_file("tiny.png"))
    spin(0.1)
    assert view._on_scroll(FakeScroll(ctrl=False), 0, -1) is False
    assert view.zoom == 1.0
    assert view._on_scroll(FakeScroll(ctrl=True), 0, -1) is True
    assert view.zoom == pytest.approx(ZOOM_STEP)
    view._on_scroll(FakeScroll(ctrl=True), 0, 1)
    assert view.zoom == pytest.approx(1.0)


def test_keyboard_shortcuts_zoom(fixture_file, shown):
    view = shown()
    view.load_path(fixture_file("tiny.png"))
    spin(0.1)
    controllers = view.observe_controllers()
    all_controllers = [controllers.get_item(i) for i in range(controllers.get_n_items())]
    (keys,) = [c for c in all_controllers if isinstance(c, Gtk.ShortcutController)]
    shortcuts = {
        keys.get_item(i).get_trigger().to_string(): keys.get_item(i) for i in range(keys.get_n_items())
    }
    zoom_in = next(s for t, s in shortcuts.items() if "plus" in t)
    zoom_in.get_action().activate(Gtk.ShortcutActionFlags(0), view, None)
    assert view.zoom == pytest.approx(ZOOM_STEP)
    fit = next(s for t, s in shortcuts.items() if "<Control>0" in t)
    fit.get_action().activate(Gtk.ShortcutActionFlags(0), view, None)
    assert view.fit


# Animation


def test_animation_plays_while_shown_and_stops_when_hidden(fixture_file, shown):
    view = shown()
    view.load_path(fixture_file("moving.webp"))  # three frames, 300 ms each
    first = view.canvas.texture
    spin(0.8)
    assert view.is_animated and view.canvas.texture is not first
    view.get_root().set_visible(False)
    spin(0.05)
    assert view._timer == 0


def test_still_image_has_no_timer(fixture_file, shown):
    view = shown()
    view.load_path(fixture_file("tiny.png"))
    spin(0.1)
    assert not view.is_animated and view._timer == 0


def test_loading_a_new_image_replaces_the_animation(fixture_file, shown):
    view = shown()
    view.load_path(fixture_file("anim.gif"))
    view.load_path(fixture_file("still.webp"))
    assert not view.is_animated and view._timer == 0 and view.image_size == (64, 64)


def test_failed_load_keeps_the_previous_image(fixture_file, shown):
    view = shown()
    view.load_path(fixture_file("tiny.png"))
    with pytest.raises(UnsupportedImage):
        view.load_path(fixture_file("opaque.xyz"))
    assert view.image_size == (64, 64)


def test_closed_view_is_freed_with_its_pixels(fixture_file):
    # Handlers kept from C must not hold the view, or every closed photo would stay in memory
    window = Gtk.Window(default_width=200, default_height=200)
    view = ImageView()
    view.load_path(fixture_file("moving.webp"))
    window.set_child(view)
    window.present()
    spin(0.2)
    gone = weakref.ref(view)
    del view
    window.set_child(None)
    window.destroy()
    del window
    spin(0.1)
    gc.collect()
    assert gone() is None
