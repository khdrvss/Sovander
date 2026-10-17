"""What a document page shows instead of a view: loading, errors, a password prompt (1.6, 3.8, 3.9, 7.2).

Adw.StatusPage reads its description as Pango markup, so file names and reasons are escaped: a file
named ``<b>&.pdf`` must show as written, not as markup.
"""

from __future__ import annotations

from collections.abc import Callable

import gi

gi.require_version("Adw", "1")
gi.require_version("Gtk", "4.0")
from gi.repository import Adw, GLib, Gtk  # noqa: E402

from sovander.i18n import _  # noqa: E402

INSTALL_COMMAND = ("sudo apt install libreoffice-writer-nogui libreoffice-calc-nogui "
                   "libreoffice-impress-nogui libreoffice-draw-nogui python3-uno")


def loading_page() -> Gtk.Widget:
    box = Gtk.Box(spacing=12, halign=Gtk.Align.CENTER, valign=Gtk.Align.CENTER, vexpand=True)
    box.append(Gtk.Spinner(spinning=True))
    box.append(Gtk.Label(label=_("Opening…")))
    return box


def error_page(name: str, reason: str) -> tuple[Gtk.Widget, str]:
    """The page and its sentence, which names the file and the reason (1.6)."""
    text = _("Cannot open “{name}”: {reason}").format(name=name, reason=reason)
    return Adw.StatusPage(icon_name="dialog-error-symbolic", title=text), text


def install_page() -> tuple[Gtk.Widget, str]:
    """Office files without the document engine: how to install it."""
    title = _("Install the document engine")
    text = _("Office files need the document engine. Install it with: {command}")
    text = text.format(command=INSTALL_COMMAND)
    page = Adw.StatusPage(icon_name="dialog-warning-symbolic", title=title,
                          description=GLib.markup_escape_text(text))
    return page, title


def unsupported_page(name: str, on_view_as_text: Callable) -> Gtk.Widget:
    """Not a known format and not text: say so and offer View as Text (7.2)."""
    page = Adw.StatusPage(icon_name="dialog-question-symbolic", title=_("This format is not supported"),
                          description=GLib.markup_escape_text(name))
    button = Gtk.Button(label=_("View as Text"), halign=Gtk.Align.CENTER)
    button.add_css_class("pill")
    button.connect("clicked", on_view_as_text)
    page.set_child(button)
    return page


def password_page(wrong: bool, on_submit: Callable) -> tuple[Gtk.Widget, Gtk.PasswordEntry]:
    """Ask for a password; after a wrong one, say so (3.8, 3.9)."""
    hint = _("That password did not work.") if wrong else _("Enter its password to open it.")
    page = Adw.StatusPage(icon_name="dialog-password-symbolic", title=_("This file is password protected"),
                          description=GLib.markup_escape_text(hint))
    entry = Gtk.PasswordEntry(show_peek_icon=True, placeholder_text=_("Password"))
    button = Gtk.Button(label=_("Open"))
    button.add_css_class("suggested-action")
    entry.connect("activate", on_submit)
    button.connect("clicked", on_submit)
    box = Gtk.Box(spacing=8, halign=Gtk.Align.CENTER)
    box.append(entry)
    box.append(button)
    page.set_child(box)
    return page, entry


def note_bar(text: str) -> tuple[Gtk.Widget, Gtk.Label]:
    """A note above a view, such as "macros were not run" (10.2)."""
    bar = Gtk.ActionBar()
    label = Gtk.Label(label=text, xalign=0, wrap=True, hexpand=True)
    bar.pack_start(Gtk.Image(icon_name="dialog-information-symbolic"))
    bar.pack_start(label)
    return bar, label
