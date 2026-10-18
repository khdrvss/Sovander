"""Text view (Requirements 2.9, 7.1, 7.3): text in its detected encoding, read-only, loaded in pages.

GtkSourceView 5 adds line numbers and highlighting where it is installed; without it a plain
Gtk.TextView shows the same text. Files over one page (5 MB by default) show their first page and
a Show more button that appends the next.
"""

from __future__ import annotations

import codecs
import logging
from collections.abc import Callable
from pathlib import Path

import gi

gi.require_version("Gtk", "4.0")
from gi.repository import Gio, Gtk  # noqa: E402

try:
    gi.require_version("GtkSource", "5")
    from gi.repository import GtkSource  # noqa: E402
except (ValueError, ImportError):
    GtkSource = None

try:
    import charset_normalizer
except ImportError:
    charset_normalizer = None

from sovander import i18n  # noqa: E402
from sovander.i18n import N_, _  # noqa: E402
from sovander.views import weak_handler  # noqa: E402

log = logging.getLogger(__name__)

MB = 1000 * 1000  # the unit the file manager shows sizes in
DEFAULT_PAGE_BYTES = 5 * MB

SHOWN_AS_TEXT = N_("Shown as text, unknown format")

_BOMS = (
    (codecs.BOM_UTF32_LE, "utf-32"),
    (codecs.BOM_UTF32_BE, "utf-32"),
    (codecs.BOM_UTF8, "utf-8-sig"),
    (codecs.BOM_UTF16_LE, "utf-16"),
    (codecs.BOM_UTF16_BE, "utf-16"),
)


def detect_encoding(sample: bytes) -> str:
    """The encoding of ``sample``: byte order mark, then strict UTF-8, then charset_normalizer.

    The whole first page is the sample (about 60 ms for 5 MB), so a file that starts in plain
    ASCII is still judged by its later letters. Codecs chosen for a mark consume it.
    """
    for bom, name in _BOMS:
        if sample.startswith(bom):
            return name
    try:
        # Not final: a sample may end in the middle of a character
        codecs.getincrementaldecoder("utf-8")().decode(sample, final=False)
        return "utf-8"
    except UnicodeDecodeError:
        pass
    if charset_normalizer is not None:
        best = charset_normalizer.from_bytes(sample).best()
        if best is not None:
            return codecs.lookup(best.encoding).name
    return "cp1252"


class TextView(Gtk.Box):
    """Read-only text with a note bar on top and a Show more bar below."""

    __gtype_name__ = "SovanderTextView"

    def __init__(self, page_bytes: int = DEFAULT_PAGE_BYTES):
        super().__init__(orientation=Gtk.Orientation.VERTICAL)
        self.page_bytes = max(1, int(page_bytes))
        self._read: Callable[[int, int], bytes] | None = None
        self._size = 0
        self._offset = 0
        self._encoding = "utf-8"
        self._decoder = None
        self._note: tuple[str, str | None, Callable[[], None] | None] | None = None

        self._note_bar = Gtk.ActionBar()
        self._note_label = Gtk.Label(xalign=0, wrap=True, hexpand=True)
        self._note_button = Gtk.Button(visible=False)
        # Handlers hold the view weakly: GTK keeps them from C, where a closed view would never be freed
        self._note_button.connect("clicked", weak_handler(self._on_note_action))
        self._note_bar.pack_start(self._note_label)
        self._note_bar.pack_end(self._note_button)
        self._note_bar.set_revealed(False)
        self.append(self._note_bar)

        self.view, self.buffer = _make_view()
        self.append(Gtk.ScrolledWindow(child=self.view, hexpand=True, vexpand=True))

        self._more_bar = Gtk.ActionBar()
        self._more_label = Gtk.Label(xalign=0, hexpand=True)
        self._more_button = Gtk.Button(label=_("Show more"))
        self._more_button.connect("clicked", weak_handler(self._on_more_clicked))
        self._more_bar.pack_start(self._more_label)
        self._more_bar.pack_end(self._more_button)
        self._more_bar.set_revealed(False)
        self.append(self._more_bar)

        i18n.add_listener(self._relabel)

    # Loading

    def load_path(self, path: Path | str) -> None:
        """Show a file from disk; OSError propagates for the caller's message (1.6)."""
        path = Path(path)
        size = path.stat().st_size

        def read(offset: int, count: int) -> bytes:
            with open(path, "rb") as f:
                f.seek(offset)
                return f.read(count)

        self._start(read, size, path.name)

    def load_bytes(self, data: bytes, name: str | None = None) -> None:
        """Show text held in memory, such as a zip entry (8.3)."""
        view = memoryview(data)
        self._start(lambda offset, count: bytes(view[offset : offset + count]), len(data), name)

    def show_more(self) -> bool:
        """Append the next page; False when everything is already shown (7.3)."""
        if not self.has_more:
            return False
        chunk = self._read(self._offset, self.page_bytes)
        if not chunk:  # the file shrank since it was opened
            self._size = self._offset
        self._append(chunk)
        return True

    @property
    def encoding(self) -> str:
        return self._encoding

    @property
    def has_more(self) -> bool:
        return self._read is not None and self._offset < self._size

    @property
    def shown_bytes(self) -> int:
        return self._offset

    @property
    def total_bytes(self) -> int:
        return self._size

    @property
    def has_line_numbers(self) -> bool:
        return GtkSource is not None and self.view.get_show_line_numbers()

    @property
    def text(self) -> str:
        return self.buffer.get_text(self.buffer.get_start_iter(), self.buffer.get_end_iter(), True)

    # Notes (7.1)

    def show_note(
        self, message: str, action_label: str | None = None, on_action: Callable[[], None] | None = None
    ) -> None:
        """Show a note above the text, with an optional button.

        Pass English source strings marked with N_(): they are translated when shown and again
        after a language change.
        """
        self._note = (message, action_label, on_action)
        self._relabel()
        self._note_bar.set_revealed(True)

    def hide_note(self) -> None:
        self._note = None
        self._note_bar.set_revealed(False)

    @property
    def note(self) -> str | None:
        """The note as shown, or None."""
        return self._note_label.get_text() if self._note else None

    # Internals

    def _start(self, read: Callable[[int, int], bytes], size: int, name: str | None) -> None:
        self._read, self._size, self._offset = read, size, 0
        first = read(0, self.page_bytes)
        self._encoding = detect_encoding(first)
        self._decoder = codecs.getincrementaldecoder(self._encoding)(errors="replace")
        self.buffer.set_text("")
        _highlight(self.buffer, name, first[:4096])
        self._append(first)

    def _append(self, chunk: bytes) -> None:
        self._offset += len(chunk)
        text = self._decoder.decode(chunk, final=self._offset >= self._size)
        # GTK text is NUL-terminated C text; show the control picture instead of cutting the text
        self.buffer.insert(self.buffer.get_end_iter(), text.replace("\0", "␀"))
        self._update_more()

    def _update_more(self) -> None:
        self._more_bar.set_revealed(self.has_more)
        self._more_label.set_text(
            _("Showing {shown} of {total}").format(
                shown=_megabytes(self._offset), total=_megabytes(self._size)
            )
        )

    def _relabel(self, _language: str | None = None) -> None:
        self._more_button.set_label(_("Show more"))
        self._update_more()
        if self._note:
            message, action_label, _on_action = self._note
            self._note_label.set_text(_(message))
            self._note_button.set_label(_(action_label) if action_label else "")
            self._note_button.set_visible(action_label is not None)

    def _on_more_clicked(self, _button) -> None:
        self.show_more()

    def _on_note_action(self, _button) -> None:
        if self._note and self._note[2]:
            self._note[2]()



def _line_numbers_are_safe() -> bool:
    """GtkSourceView before 5.21 keeps a view with line numbers alive for good. Before 5.12.1 (Ubuntu
    24.04 has 5.12.0) its gutter renderers held the view strongly ("switch to a weak ref for the
    GtkSourceView backpointer ... fixes a potential leak", 5.12.1); until 5.21 so did the lines the
    gutter last drew ("break a circular reference when line numbers are enabled"). Every closed text
    file stayed in memory with its whole buffer, so line numbers wait for the fixed library."""
    version = (GtkSource.get_major_version(), GtkSource.get_minor_version(), GtkSource.get_micro_version())
    return version >= (5, 21, 0)

def _make_view():
    if GtkSource is not None:
        buffer = GtkSource.Buffer()
        view = GtkSource.View(buffer=buffer, show_line_numbers=_line_numbers_are_safe())
    else:
        buffer = Gtk.TextBuffer()
        view = Gtk.TextView(buffer=buffer)
    buffer.set_enable_undo(False)
    view.set_editable(False)
    view.set_monospace(True)
    view.set_wrap_mode(Gtk.WrapMode.WORD_CHAR)
    for side in ("left", "right", "top", "bottom"):
        getattr(view, f"set_{side}_margin")(8)
    return view, buffer


def _highlight(buffer, name: str | None, head: bytes) -> None:
    if GtkSource is None:
        return
    try:
        content_type, _uncertain = Gio.content_type_guess(name, head)
        language = GtkSource.LanguageManager.get_default().guess_language(name, content_type)
        buffer.set_language(language)
        buffer.set_highlight_syntax(language is not None)
    except Exception:  # noqa: BLE001 - highlighting is a nicety; plain text is always shown
        log.exception("syntax highlighting failed")


def _megabytes(count: int) -> str:
    return _("{size} MB").format(size=f"{count / MB:.1f}")
