"""Image view (Requirement 2.7): zoom, best fit, Ctrl+scroll and pinch, animation, EXIF orientation.

Images are decoded by GdkPixbuf loaders from bytes, never from a path, so an SVG has no base
folder to pull neighbouring files from (10.3). HEIC and AVIF open where their loaders are
installed. Drawing is a small canvas widget rather than Gtk.Picture, which on GTK 4.6 cannot show
an image smaller than its natural size inside a scrolled window.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

import gi

gi.require_version("Gtk", "4.0")
gi.require_version("Gdk", "4.0")
gi.require_version("GdkPixbuf", "2.0")
gi.require_version("Graphene", "1.0")
from gi.repository import Gdk, GdkPixbuf, GLib, GObject, Graphene, Gtk  # noqa: E402

from sovander.i18n import N_, _  # noqa: E402
from sovander.views import weak_handler  # noqa: E402

MAX_PIXELS = 150_000_000  # bigger images are refused before their pixels are allocated
MAX_SIDE = 16384  # bigger images are scaled down while decoding: older GPUs stop at this texture size
CHUNK = 1024 * 1024
ZOOM_STEP = 1.25
ZOOM_MIN, ZOOM_MAX = 0.02, 32.0
MIN_FRAME_MS = 20


class ImageError(Exception):
    """Why an image cannot be shown; ``str()`` gives the sentence in the current language."""

    text = N_("This image cannot be shown")

    def __init__(self, **details):
        super().__init__(self.text)
        self.details = details

    def __str__(self) -> str:
        return _(self.text).format(**self.details)


class UnsupportedImage(ImageError):
    text = N_("This image format is not supported")

    def __init__(self, format_name: str | None = None):
        super().__init__(format=format_name or "")
        if format_name:
            self.text = N_("No decoder for {format} images is installed")


class DamagedImage(ImageError):
    text = N_("This image is damaged")


class ImageTooLarge(ImageError):
    text = N_("This image is too large to open safely")


@dataclass
class Decoded:
    pixbuf: GdkPixbuf.Pixbuf  # the still image, turned upright, or an animation's first frame
    animation: GdkPixbuf.PixbufAnimation | None
    format_name: str


def decode(data: bytes) -> Decoded:
    """Decode an image by its content (2.11); raises an ImageError subclass."""
    if not data:
        raise DamagedImage()
    try:
        return _decode(data, None)
    except UnsupportedImage:
        # gdk-pixbuf picks a loader by MIME type; shared-mime-info calls APNG image/apng,
        # which the PNG loader does not claim, though it reads the first frame fine
        fallback = _loader_by_magic(data)
        if fallback is None:
            raise
        return _decode(data, fallback)


def anchored_scroll(value: float, anchor: float, old: float, new: float, image: float, view: float) -> float:
    """Scroll position that keeps the image point under ``anchor`` in place when zoom goes old→new.

    Lengths are along one axis: ``image`` at zoom 1, ``view`` the visible length. An image smaller
    than the view is centred, which the offsets account for.
    """
    point = (value + anchor - max(0.0, (view - image * old) / 2)) / old
    length = image * new
    target = point * new + max(0.0, (view - length) / 2) - anchor
    return min(max(0.0, target), max(0.0, length - view))


class _Canvas(Gtk.Widget):
    """Draws one texture at a zoom, or at best fit inside whatever space it is given."""

    __gtype_name__ = "SovanderImageCanvas"

    def __init__(self, on_scale):
        super().__init__()
        self.texture: Gdk.Texture | None = None
        self.zoom = 1.0
        self.fit = True
        self.scale = 1.0
        self._on_scale = on_scale

    def fit_scale(self, width: int, height: int) -> float:
        if self.texture is None:
            return 1.0
        tw, th = self.texture.get_width(), self.texture.get_height()
        return min(width / tw, height / th, 1.0) if width > 0 and height > 0 else 1.0

    def do_measure(self, orientation, _for_size):
        if self.texture is None or self.fit:
            return 0, 0, -1, -1
        size = (
            self.texture.get_width()
            if orientation == Gtk.Orientation.HORIZONTAL
            else self.texture.get_height()
        )
        length = max(1, round(size * self.zoom))
        return length, length, -1, -1

    def do_size_allocate(self, width, height, _baseline):
        scale = self.fit_scale(width, height) if self.fit else self.zoom
        if scale != self.scale:
            self.scale = scale
            # Told after layout: a listener relabelling a widget must not resize during allocation
            GLib.idle_add(self._on_scale)

    def do_snapshot(self, snapshot):
        if self.texture is None:
            return
        w, h = self.get_width(), self.get_height()
        dw, dh = self.texture.get_width() * self.scale, self.texture.get_height() * self.scale
        rect = Graphene.Rect().init(max(0.0, (w - dw) / 2), max(0.0, (h - dh) / 2), dw, dh)
        snapshot.append_texture(self.texture, rect)


class ImageView(Gtk.Box):
    """A scrollable, zoomable image; starts at best fit (never enlarged past 100%)."""

    __gtype_name__ = "SovanderImageView"

    zoom = GObject.Property(type=float, default=1.0)  # the scale on screen, also while fitting
    fit = GObject.Property(type=bool, default=True)

    def __init__(self):
        super().__init__(orientation=Gtk.Orientation.VERTICAL)
        self.decoded: Decoded | None = None
        self._iter = None
        self._timer = 0
        self._pointer: tuple[float, float] | None = None
        self._pinch_start = 1.0

        # No handler below holds the view itself: GTK keeps handlers from C, out of the garbage
        # collector's sight, so one that did would keep a closed view and its pixels forever
        self.canvas = _Canvas(weak_handler(self._scale_changed))
        self.scroller = Gtk.ScrolledWindow(child=self.canvas, hexpand=True, vexpand=True)
        self.scroller.set_focusable(True)
        self.append(self.scroller)

        scroll = Gtk.EventControllerScroll(flags=Gtk.EventControllerScrollFlags.VERTICAL)
        scroll.set_propagation_phase(Gtk.PropagationPhase.CAPTURE)
        scroll.connect("scroll", weak_handler(self._on_scroll))
        self.scroller.add_controller(scroll)

        motion = Gtk.EventControllerMotion()
        motion.connect("motion", weak_handler(self._on_motion))
        motion.connect("leave", weak_handler(self._on_leave))
        self.scroller.add_controller(motion)

        pinch = Gtk.GestureZoom()
        pinch.connect("begin", weak_handler(self._on_pinch_begin))
        pinch.connect("scale-changed", weak_handler(self._on_pinch))
        self.scroller.add_controller(pinch)

        keys = Gtk.ShortcutController()
        for trigger, act in (
            ("<Control>plus|<Control>equal|<Control>KP_Add", ImageView.zoom_in),
            ("<Control>minus|<Control>KP_Subtract", ImageView.zoom_out),
            ("<Control>0|<Control>KP_0", ImageView.fit_to_window),
            ("<Control>1|<Control>KP_1", lambda view: view.set_zoom(1.0)),
        ):
            # A callback action is handed the widget, so it needs no reference of its own
            action = Gtk.CallbackAction.new(lambda widget, _args, act=act: act(widget) or True)
            keys.add_shortcut(Gtk.Shortcut(trigger=Gtk.ShortcutTrigger.parse_string(trigger), action=action))
        self.add_controller(keys)

        self.connect("map", ImageView._play)
        self.connect("unmap", ImageView._stop)

    # Loading

    def load_path(self, path: Path | str) -> None:
        """Show an image file; OSError propagates for the caller's message (1.6)."""
        self.load_bytes(Path(path).read_bytes())

    def load_bytes(self, data: bytes) -> None:
        """Show an image held in memory; raises an ImageError subclass when it cannot."""
        decoded = decode(data)
        self._stop()
        self.decoded = decoded
        self._iter = decoded.animation.get_iter(None) if decoded.animation else None
        self._show(decoded.pixbuf)
        self.fit_to_window()
        if self.get_mapped():
            self._play()

    @property
    def is_animated(self) -> bool:
        return self._iter is not None

    @property
    def image_size(self) -> tuple[int, int]:
        texture = self.canvas.texture
        return (texture.get_width(), texture.get_height()) if texture else (0, 0)

    # Zoom

    def zoom_in(self) -> None:
        self.set_zoom(self.zoom * ZOOM_STEP)

    def zoom_out(self) -> None:
        self.set_zoom(self.zoom / ZOOM_STEP)

    def fit_to_window(self) -> None:
        self.canvas.fit = True
        self.fit = True
        width, height = self.scroller.get_width(), self.scroller.get_height()
        if width > 0 and height > 0:
            self._set_scale(self.canvas.fit_scale(width, height))
        self.canvas.queue_resize()

    def set_zoom(self, zoom: float, anchor: tuple[float, float] | None = None) -> None:
        """Zoom to ``zoom``, keeping the image point under ``anchor`` (default: the centre) still."""
        if self.canvas.texture is None:
            return
        new = min(max(zoom, ZOOM_MIN), ZOOM_MAX)
        old = self.canvas.scale
        hadj, vadj = self.scroller.get_hadjustment(), self.scroller.get_vadjustment()
        if anchor is None:
            anchor = (hadj.get_page_size() / 2, vadj.get_page_size() / 2)
        size = self.image_size
        for adj, point, length in ((hadj, anchor[0], size[0]), (vadj, anchor[1], size[1])):
            page = adj.get_page_size()
            value = anchored_scroll(adj.get_value(), point, old, new, length, page)
            # The viewport sets the same upper bound at its next layout; setting it now lets the value stick
            adj.configure(value, 0, max(length * new, page), adj.get_step_increment(), page * 0.9, page)
        self.canvas.fit = False
        self.canvas.zoom = new
        self.fit = False
        self._set_scale(new)
        self.canvas.queue_resize()

    # Internals

    def _set_scale(self, scale: float) -> None:
        """Take a new scale at once, so repeated zoom steps never start from a stale one."""
        self.canvas.scale = scale
        if self.zoom != scale:
            self.zoom = scale

    def _show(self, pixbuf: GdkPixbuf.Pixbuf) -> None:
        self.canvas.texture = Gdk.Texture.new_for_pixbuf(pixbuf)
        self.canvas.queue_resize()
        self.canvas.queue_draw()

    def _scale_changed(self) -> bool:
        self._set_scale(self.canvas.scale)
        return GLib.SOURCE_REMOVE

    def _play(self) -> None:
        if self._iter is not None and not self._timer:
            self._schedule()

    def _stop(self) -> None:
        if self._timer:
            GLib.source_remove(self._timer)
            self._timer = 0

    def _schedule(self) -> None:
        delay = self._iter.get_delay_time()
        self._timer = GLib.timeout_add(max(delay, MIN_FRAME_MS), self._next_frame) if delay >= 0 else 0

    def _next_frame(self) -> bool:
        self._timer = 0
        if self._iter is None:
            return GLib.SOURCE_REMOVE
        if self._iter.advance(None):
            # The loader may reuse the frame's memory for the next one; a texture must not change
            self._show(self._iter.get_pixbuf().copy())
        self._schedule()
        return GLib.SOURCE_REMOVE

    def _on_scroll(self, controller, _dx, dy) -> bool:
        if not controller.get_current_event_state() & Gdk.ModifierType.CONTROL_MASK or dy == 0:
            return False
        self.set_zoom(self.zoom * ZOOM_STEP ** (-dy), self._pointer)
        return True

    def _on_motion(self, _controller, x, y) -> None:
        self._pointer = (x, y)

    def _on_leave(self, _controller) -> None:
        self._pointer = None

    def _on_pinch_begin(self, _gesture, _sequence) -> None:
        self._pinch_start = self.zoom

    def _on_pinch(self, gesture, scale) -> None:
        ok, x, y = gesture.get_bounding_box_center()
        self.set_zoom(self._pinch_start * scale, (x, y) if ok else None)



def _decode(data: bytes, type_name: str | None) -> Decoded:
    try:
        loader = GdkPixbuf.PixbufLoader.new_with_type(type_name) if type_name else GdkPixbuf.PixbufLoader()
    except GLib.Error as e:
        raise UnsupportedImage(_format_label(data)) from e
    refused = []

    def on_size(_loader, width, height):
        if width * height > MAX_PIXELS:
            refused.append((width, height))
            # Zero is gdk-pixbuf's "stop, allocate nothing"; any other size is allocated in full first
            loader.set_size(0, 0)
        elif max(width, height) > MAX_SIDE:
            shrink = MAX_SIDE / max(width, height)
            loader.set_size(max(1, round(width * shrink)), max(1, round(height * shrink)))

    loader.connect("size-prepared", on_size)
    try:
        # In chunks, so that an oversized image stops being read once its header is known
        for start in range(0, len(data), CHUNK):
            loader.write(data[start : start + CHUNK])
            if refused:
                break
        loader.close()
    except GLib.Error as e:
        _close_quietly(loader)
        if refused:
            raise ImageTooLarge() from e
        if e.matches(GdkPixbuf.pixbuf_error_quark(), GdkPixbuf.PixbufError.UNKNOWN_TYPE) or e.matches(
            GdkPixbuf.pixbuf_error_quark(), GdkPixbuf.PixbufError.UNSUPPORTED_OPERATION
        ):
            raise UnsupportedImage(_format_label(data)) from e
        raise DamagedImage() from e
    if refused:
        raise ImageTooLarge()
    pixbuf, animation = loader.get_pixbuf(), loader.get_animation()
    if pixbuf is None:
        raise DamagedImage()
    if animation is None or animation.is_static_image():
        animation = None
        pixbuf = pixbuf.apply_embedded_orientation() or pixbuf
    fmt = loader.get_format()
    return Decoded(pixbuf, animation, fmt.get_name() if fmt else (type_name or ""))


def _close_quietly(loader) -> None:
    try:
        loader.close()
    except GLib.Error:
        pass


_MAGIC = (
    (b"\x89PNG\r\n\x1a\n", "png"),
    (b"GIF8", "gif"),
    (b"\xff\xd8\xff", "jpeg"),
    (b"BM", "bmp"),
    (b"II*\x00", "tiff"),
    (b"MM\x00*", "tiff"),
)


def _loader_by_magic(data: bytes) -> str | None:
    if data[:4] == b"RIFF" and data[8:12] == b"WEBP":
        return "webp"
    return next((name for magic, name in _MAGIC if data.startswith(magic)), None)


def _format_label(data: bytes) -> str | None:
    """A name for the error message: HEIC and AVIF are the formats whose loaders may be missing."""
    if data[4:8] == b"ftyp":
        brand = data[8:12]
        if brand in (b"avif", b"avis"):
            return "AVIF"
        if brand in (b"heic", b"heix", b"hevc", b"hevx", b"mif1", b"msf1", b"heim", b"heis"):
            return "HEIC"
    name = _loader_by_magic(data)
    return name.upper() if name else None
