"""Archive browser (Requirements 8.1, 8.2, 8.4, 8.6, 10.4): a zip shown like a folder.

Folders come first, then files, with sizes and dates; Up, Alt+Up and Backspace go to the parent
folder, and the path bar jumps to any folder above. Opening a file reads its bytes into memory on
a worker thread (``Archive.read``) and hands them to ``on_open_entry(name, data)``, which routes
them to a viewer. Nothing is written to disk (8.3). A locked entry asks for its password, says so
when it is wrong and asks again (8.4); the password that worked is tried first on the next entry.
The Name Encoding menu reads every name again in the code page the user picks (8.6).
"""

from __future__ import annotations

import functools
import logging
import threading
import weakref
from collections.abc import Callable

import gi

gi.require_version("Gio", "2.0")
gi.require_version("Gtk", "4.0")
gi.require_version("Pango", "1.0")
from gi.repository import Gio, GLib, GObject, Gtk, Pango  # noqa: E402

from sovander import i18n  # noqa: E402
from sovander.archive import (  # noqa: E402
    Archive,
    ArchiveError,
    Damaged,
    Entry,
    NeedsPassword,
    TooLarge,
    Unsupported,
    WrongPassword,
)
from sovander.i18n import N_, _  # noqa: E402
from sovander.views import to_main_loop, weak_handler  # noqa: E402
from sovander.zipnames import CHOICES  # noqa: E402

log = logging.getLogger(__name__)

AUTOMATIC = ""  # the encoding action's target for "guess again"

_ENCODING_NAMES = {
    "utf-8": N_("Unicode"),
    "cp866": N_("Cyrillic, DOS"),
    "cp1251": N_("Cyrillic, Windows"),
    "cp437": N_("Western, DOS (US)"),
    "cp850": N_("Western, DOS"),
    "gbk": N_("Chinese, Simplified"),
    "cp932": N_("Japanese"),
    "cp950": N_("Chinese, Traditional"),
    "cp949": N_("Korean"),
    "cp852": N_("Central European, DOS"),
    "cp737": N_("Greek, DOS"),
    "cp857": N_("Turkish, DOS"),
    "cp862": N_("Hebrew, DOS"),
    "cp720": N_("Arabic, DOS"),
    "cp874": N_("Thai"),
    "cp775": N_("Baltic, DOS"),
    "cp1258": N_("Vietnamese"),
}


def encoding_label(encoding: str) -> str:
    """ "Cyrillic, DOS (CP866)" for a menu."""
    name = _ENCODING_NAMES.get(encoding)
    return f"{_(name)} ({encoding.upper()})" if name else encoding.upper()


def error_message(error: BaseException, name: str) -> str:
    """The sentence shown when an entry cannot be read."""
    if isinstance(error, (TooLarge, MemoryError)):
        return _("Too large to open safely")
    if isinstance(error, Unsupported):
        return _("Sovander cannot unpack this file: its compression or encryption is not supported")
    if isinstance(error, Damaged):
        return _("This file is damaged")
    reason = getattr(error, "strerror", None) or str(error)
    return _("Cannot open “{name}”: {reason}").format(name=name, reason=reason)


def _hidden_junk(entry: Entry) -> bool:
    """macOS's resource forks: never what anyone sent."""
    return entry.path.partition("/")[0] == "__MACOSX" or entry.name.startswith("._")


class _Row(GObject.Object):
    __gtype_name__ = "SovanderArchiveRow"

    def __init__(self, entry: Entry):
        super().__init__()
        self.entry = entry


class PasswordDialog(Gtk.Window):
    """Asks for an entry's password; ``done(password)`` gets the text, or None on Cancel or Escape."""

    __gtype_name__ = "SovanderPasswordDialog"

    def __init__(self, parent: Gtk.Window | None, name: str, wrong: bool, done: Callable[[str | None], None]):
        super().__init__(title=_("This file is password protected"), modal=True, transient_for=parent,
                         resizable=False, default_width=380)
        self._done: Callable[[str | None], None] | None = done
        text = Gtk.Label(label=_("Enter its password to open it.") + f"\n{name}", wrap=True, xalign=0,
                         max_width_chars=44)
        self.entry = Gtk.PasswordEntry(show_peek_icon=True, hexpand=True)
        self.entry.connect("activate", weak_handler(self.accept))
        self.error = Gtk.Label(label=_("That password did not work."), xalign=0, visible=wrong,
                               css_classes=["error"])
        cancel = Gtk.Button(label=_("Cancel"))
        cancel.connect("clicked", weak_handler(self.cancel))
        ok = Gtk.Button(label=_("Open"), css_classes=["suggested-action"])
        ok.connect("clicked", weak_handler(self.accept))
        buttons = Gtk.Box(spacing=12, halign=Gtk.Align.END, homogeneous=True)
        buttons.append(cancel)
        buttons.append(ok)
        box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=12, margin_top=18, margin_bottom=18,
                      margin_start=18, margin_end=18)
        for part in (text, self.entry, self.error, buttons):
            box.append(part)
        self.set_child(box)
        keys = Gtk.ShortcutController()
        keys.add_shortcut(Gtk.Shortcut(trigger=Gtk.ShortcutTrigger.parse_string("Escape"),
                                       action=Gtk.NamedAction.new("window.close")))
        self.add_controller(keys)
        self.connect("close-request", weak_handler(self._on_close_request))
        self.entry.grab_focus()

    def accept(self, *_args) -> None:
        self._finish(self.entry.get_text())

    def cancel(self, *_args) -> None:
        self._finish(None)

    def _finish(self, password: str | None) -> None:
        done, self._done = self._done, None
        self.close()
        if done is not None:
            done(password)

    def _on_close_request(self, *_args) -> bool:
        done, self._done = self._done, None
        if done is not None:
            done(None)
        return False


class ArchiveView(Gtk.Box):
    """A zip's folders and files. The view owns ``archive`` and closes it when it goes."""

    __gtype_name__ = "SovanderArchiveView"

    def __init__(self, archive: Archive, on_open_entry: Callable[[str, bytes], None], name: str = ""):
        super().__init__(orientation=Gtk.Orientation.VERTICAL)
        self.archive = archive
        self._on_open_entry = on_open_entry
        self._name = name
        self._folder = ""
        self._password: str | None = None
        self._busy = False
        self.password_dialog: PasswordDialog | None = None
        weakref.finalize(self, archive.close)

        self._up = Gtk.Button(icon_name="go-up-symbolic", action_name="archive.up")
        self._crumbs = Gtk.Box(css_classes=["linked"])
        crumbs = Gtk.ScrolledWindow(child=self._crumbs, hexpand=True, vscrollbar_policy=Gtk.PolicyType.NEVER,
                                    propagate_natural_height=True)
        self._spinner = Gtk.Spinner()
        self._encoding = Gtk.MenuButton(sensitive=archive.name_encoding is not None)
        bar = Gtk.Box(spacing=6, margin_top=6, margin_bottom=6, margin_start=6, margin_end=6)
        for part in (self._up, crumbs, self._spinner, self._encoding):
            bar.append(part)
        self.append(bar)

        self._message = Gtk.Label(xalign=0, wrap=True, hexpand=True)
        self._dismiss = Gtk.Button(icon_name="window-close-symbolic", css_classes=["flat"])
        self._dismiss.connect("clicked", weak_handler(self._on_dismiss))
        self._message_bar = Gtk.ActionBar()
        self._message_bar.pack_start(self._message)
        self._message_bar.pack_end(self._dismiss)
        self._message_bar.set_revealed(False)
        self.append(self._message_bar)

        self._rows = Gio.ListStore(item_type=_Row)
        self.list = Gtk.ColumnView(model=Gtk.SingleSelection(model=self._rows), vexpand=True,
                                   css_classes=["data-table"])
        self._columns = []
        for setup, bind, expand in ((_setup_name, _bind_name, True), (_setup_text, _bind_size, False),
                                    (_setup_text, _bind_date, False)):
            factory = Gtk.SignalListItemFactory()
            factory.connect("setup", setup)
            factory.connect("bind", bind)
            column = Gtk.ColumnViewColumn(factory=factory, expand=expand)
            self._columns.append(column)
            self.list.append_column(column)
        self.list.connect("activate", weak_handler(self._on_activate))
        self.append(Gtk.ScrolledWindow(child=self.list, vexpand=True, hexpand=True))

        self._actions = Gio.SimpleActionGroup()
        up = Gio.SimpleAction.new("up", None)
        up.connect("activate", weak_handler(self._on_up))
        self._actions.add_action(up)
        encoding = Gio.SimpleAction.new_stateful(
            "encoding", GLib.VariantType.new("s"), GLib.Variant.new_string(AUTOMATIC)
        )
        encoding.connect("change-state", weak_handler(self._on_encoding))
        self._actions.add_action(encoding)
        self.insert_action_group("archive", self._actions)

        keys = Gtk.ShortcutController(scope=Gtk.ShortcutScope.LOCAL)
        keys.add_shortcut(Gtk.Shortcut(trigger=Gtk.ShortcutTrigger.parse_string("<Alt>Up|BackSpace"),
                                       action=Gtk.NamedAction.new("archive.up")))
        self.add_controller(keys)

        i18n.add_listener(self._relabel)
        self._relabel()

    # Public

    @property
    def folder(self) -> str:
        """The folder shown, "" for the top of the archive."""
        return self._folder

    @property
    def busy(self) -> bool:
        """True while an entry is being read."""
        return self._busy

    @property
    def message(self) -> str | None:
        return self._message.get_label() if self._message_bar.get_revealed() else None

    def shown(self) -> list[Entry]:
        """The entries listed now, in order."""
        return [self._rows.get_item(i).entry for i in range(self._rows.get_n_items())]

    def show_folder(self, folder: str) -> None:
        self._folder = folder.strip("/")
        entries = [e for e in self.archive.list(self._folder) if not _hidden_junk(e)]
        self._rows.splice(0, self._rows.get_n_items(), [_Row(e) for e in entries])
        self._actions.lookup_action("up").set_enabled(bool(self._folder))
        self._rebuild_crumbs()

    def go_up(self) -> None:
        if self._folder:
            self.show_folder(self._folder.rpartition("/")[0])

    def open_entry(self, entry: Entry, password: str | None = None) -> bool:
        """A folder opens in place; a file is read in the background, then handed on. False while busy."""
        if entry.is_dir:
            self.show_folder(entry.path)
            return True
        if self._busy:
            return False
        if not entry.supported:
            self.show_message(error_message(Unsupported(entry.path), entry.name))
            return True
        self._set_busy(True)
        typed = password is not None
        password = password if typed else (self._password if entry.encrypted else None)
        deliver = functools.partial(weak_handler(self._on_read), entry, password, typed)
        threading.Thread(target=_read, args=(self.archive, entry, password, deliver), daemon=True,
                         name="sovander-archive").start()
        return True

    def set_name_encoding(self, encoding: str | None) -> None:
        """Read the names again in ``encoding``, or guess again with None (8.6)."""
        self._actions.lookup_action("encoding").set_state(GLib.Variant.new_string(encoding or AUTOMATIC))
        self.archive.set_name_encoding(encoding or None)
        # A folder's own name may read differently now; the top is always there
        self.show_folder(self._folder if self.archive.list(self._folder) else "")

    def show_message(self, text: str) -> None:
        self._message.set_label(text)
        self._message_bar.set_revealed(True)

    def close(self) -> None:
        """Close the archive now rather than when the view is freed."""
        self.archive.close()

    # Reading

    def _on_read(self, entry: Entry, password: str | None, typed: bool, result) -> bool:
        self._set_busy(False)
        data, error = result
        if data is not None:
            if password is not None:
                self._password = password
            self._message_bar.set_revealed(False)
            self._on_open_entry(entry.name, data)
        elif isinstance(error, NeedsPassword):
            self._ask_password(entry, wrong=False)
        elif isinstance(error, WrongPassword):
            if not typed:
                self._password = None  # this entry has a password of its own
            self._ask_password(entry, wrong=typed)
        else:
            log.info("cannot read %s: %s", entry.path, error)
            self.show_message(error_message(error, entry.name))
        return GLib.SOURCE_REMOVE

    def _ask_password(self, entry: Entry, wrong: bool) -> None:
        root = self.get_root()
        self.password_dialog = PasswordDialog(
            root if isinstance(root, Gtk.Window) else None, entry.name, wrong,
            functools.partial(weak_handler(self._on_password), entry),
        )
        self.password_dialog.present()

    def _on_password(self, entry: Entry, password: str | None) -> None:
        self.password_dialog = None
        if password is not None:
            self.open_entry(entry, password)

    def _set_busy(self, busy: bool) -> None:
        self._busy = busy
        self._spinner.set_spinning(busy)

    # Handlers

    def _on_activate(self, _list, position) -> None:
        row = self._rows.get_item(position)
        if row is not None:
            self.open_entry(row.entry)

    def _on_up(self, *_args) -> None:
        self.go_up()

    def _on_crumb(self, _button, folder: str) -> None:
        self.show_folder(folder)

    def _on_encoding(self, _action, value) -> None:
        self.set_name_encoding(value.get_string() or None)

    def _on_dismiss(self, *_args) -> None:
        self._message_bar.set_revealed(False)

    def _rebuild_crumbs(self) -> None:
        while (child := self._crumbs.get_first_child()) is not None:
            self._crumbs.remove(child)
        parts = self._folder.split("/") if self._folder else []
        for depth, label in enumerate([self._name or "/", *parts]):
            button = Gtk.Button(label=label, css_classes=["flat"])
            button.connect("clicked", weak_handler(self._on_crumb), "/".join(parts[:depth]))
            self._crumbs.append(button)

    def _relabel(self, _language: str | None = None) -> None:
        self._up.set_tooltip_text(_("Up"))
        self._dismiss.set_tooltip_text(_("Hide This Message"))
        for column, title in zip(self._columns, (_("Name"), _("Size"), _("Modified")), strict=True):
            column.set_title(title)
        self._encoding.set_label(i18n.pgettext("zip archive", "Name Encoding"))
        menu = Gio.Menu()
        auto = Gio.MenuItem.new(_("Automatic"), None)
        auto.set_action_and_target_value("archive.encoding", GLib.Variant.new_string(AUTOMATIC))
        menu.append_item(auto)
        choices = Gio.Menu()
        for encoding in CHOICES:
            item = Gio.MenuItem.new(encoding_label(encoding), None)
            item.set_action_and_target_value("archive.encoding", GLib.Variant.new_string(encoding))
            choices.append_item(item)
        menu.append_section(None, choices)
        self._encoding.set_menu_model(menu)
        self.show_folder(self._folder)


def _read(archive: Archive, entry: Entry, password: str | None, deliver) -> None:
    """On the worker thread: the entry's bytes or the error, handed back on the main loop."""
    try:
        result = (archive.read(entry, password), None)
    except (ArchiveError, OSError, MemoryError, ValueError) as e:
        result = (None, e)
    to_main_loop(deliver, result)


# Cells: module functions, so no handler holds a view


def _setup_name(_factory, item) -> None:
    box = Gtk.Box(spacing=8)
    box.append(Gtk.Image())
    box.append(Gtk.Label(xalign=0, ellipsize=Pango.EllipsizeMode.MIDDLE, hexpand=True))
    item.set_child(box)


def _bind_name(_factory, item) -> None:
    entry = item.get_item().entry
    icon = item.get_child().get_first_child()
    label = icon.get_next_sibling()
    if entry.is_dir:
        icon.set_from_icon_name("folder-symbolic")
    elif entry.encrypted:
        icon.set_from_icon_name("changes-prevent-symbolic")
    else:
        guess, _uncertain = Gio.content_type_guess(entry.name, None)  # by name only: no data, no lookup
        icon.set_from_gicon(Gio.content_type_get_symbolic_icon(guess))
    label.set_label(entry.name)
    label.set_opacity(1.0 if entry.supported or entry.is_dir else 0.5)


def _setup_text(_factory, item) -> None:
    item.set_child(Gtk.Label(xalign=1, css_classes=["dim-label", "numeric"]))


def _bind_size(_factory, item) -> None:
    entry = item.get_item().entry
    item.get_child().set_label("" if entry.is_dir else GLib.format_size(entry.size))


def _bind_date(_factory, item) -> None:
    modified = item.get_item().entry.modified
    item.get_child().set_label(modified.strftime("%x %H:%M") if modified else "")
