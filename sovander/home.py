"""Home screen: the recent files, shown when Sovander starts without a file (Requirements 11.1, 11.3-11.5).

A grid of cards, newest first, each with a thumbnail (thumbs.py), the name and when it was opened.
A file that no longer exists is marked Missing and does not open (11.4). Each card's menu, a right
click, a long press or the Menu key offers Remove from List; Clear History forgets everything after
a confirmation (11.3). With history off the page says so, and the switch in its bar turns history
back on (11.5). The empty state's button triggers ``win.open``, the window's Open action.
"""

from __future__ import annotations

import time
from collections.abc import Callable
from datetime import datetime
from pathlib import Path

import gi

gi.require_version("Adw", "1")
gi.require_version("Gdk", "4.0")
gi.require_version("Gtk", "4.0")
gi.require_version("Pango", "1.0")
from gi.repository import Adw, Gdk, Gio, GLib, Gtk, Pango  # noqa: E402

from sovander import i18n, thumbs  # noqa: E402
from sovander.i18n import _  # noqa: E402
from sovander.store import Recent, Store, local_path  # noqa: E402
from sovander.views import weak_handler  # noqa: E402

OPEN_ACTION = "win.open"
CARD_WIDTH, THUMB_HEIGHT = 148, 180


def when_opened(timestamp: float, now: float | None = None) -> str:
    """When a file was opened: today or yesterday with the time, else the date in the system's format."""
    opened = datetime.fromtimestamp(timestamp)
    days = (datetime.fromtimestamp(time.time() if now is None else now).date() - opened.date()).days
    clock = opened.strftime("%H:%M")
    if days == 0:
        return _("Today, {time}").format(time=clock)
    if days == 1:
        return _("Yesterday, {time}").format(time=clock)
    return opened.strftime("%x")


class _Card(Gtk.FlowBoxChild):
    """One recent file. ``path`` is None for a missing or non-local file, which does not open."""

    __gtype_name__ = "SovanderRecentCard"

    def __init__(self, recent: Recent, menu: Gio.MenuModel):
        super().__init__()
        self.recent = recent
        self.path: Path | None = None if recent.missing else local_path(recent.uri)
        # One box for every thumbnail, the picture scaled to fit and centred: a picture's own size
        # would make a portrait page's card taller than a slide's (C6). Clamps cap what the picture
        # asks for; the size request fixes the rest.
        self._slot = Adw.Clamp(maximum_size=CARD_WIDTH, tightening_threshold=CARD_WIDTH)
        self._frame = Adw.Clamp(
            orientation=Gtk.Orientation.VERTICAL, maximum_size=THUMB_HEIGHT,
            tightening_threshold=THUMB_HEIGHT, child=self._slot, width_request=CARD_WIDTH,
            height_request=THUMB_HEIGHT, halign=Gtk.Align.CENTER, css_classes=["card"],
            overflow=Gtk.Overflow.HIDDEN,
        )
        name = Gtk.Label(label=recent.display_name, ellipsize=Pango.EllipsizeMode.MIDDLE, max_width_chars=16,
                         width_chars=16)
        when = Gtk.Label(label=when_opened(recent.opened_at), css_classes=["caption", "dim-label"])
        box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=6, width_request=CARD_WIDTH)
        for part in (self._frame, name, when):
            box.append(part)
        if recent.missing:
            self._frame.set_opacity(0.4)
            name.set_opacity(0.6)
            box.append(Gtk.Label(label=_("Missing"), css_classes=["caption", "error"]))
        self.menu_button = Gtk.MenuButton(
            icon_name="view-more-symbolic", menu_model=menu, halign=Gtk.Align.END, valign=Gtk.Align.START,
            margin_top=6, margin_end=6, css_classes=["circular", "osd"], tooltip_text=_("More Options"),
        )
        overlay = Gtk.Overlay(child=box, margin_top=6, margin_bottom=6, margin_start=6, margin_end=6)
        overlay.add_overlay(self.menu_button)
        self.set_child(overlay)
        self.set_tooltip_text(str(local_path(recent.uri) or recent.uri))
        for gesture in (Gtk.GestureClick(button=Gdk.BUTTON_SECONDARY), Gtk.GestureLongPress()):
            gesture.connect("pressed", weak_handler(self._on_menu_gesture))
            self.add_controller(gesture)
        self.show_icon()

    @property
    def shows_thumbnail(self) -> bool:
        return isinstance(self._slot.get_child(), Gtk.Picture)

    def show_icon(self) -> None:
        self._slot.set_child(Gtk.Image(icon_name=thumbs.icon_name(self.recent.kind), pixel_size=64))

    def show_thumbnail(self, png: Path) -> bool:
        try:
            texture = Gdk.Texture.new_from_filename(str(png))
        except GLib.Error:
            return False
        # can-shrink and keep-aspect-ratio are the defaults: drawn whole, centred in the box
        self._slot.set_child(Gtk.Picture(paintable=texture, alternative_text=self.recent.display_name))
        return True

    def _on_menu_gesture(self, gesture, *_args):
        gesture.set_state(Gtk.EventSequenceState.CLAIMED)
        self.menu_button.popup()


class _Confirm(Gtk.Window):
    """A small modal question with Cancel and one destructive button; Escape cancels."""

    __gtype_name__ = "SovanderConfirm"

    def __init__(self, parent: Gtk.Window | None, title: str, body: str, verb: str,
                 on_confirm: Callable[[], None]):
        super().__init__(title=title, modal=True, transient_for=parent, resizable=False, default_width=380)
        self._on_confirm: Callable[[], None] | None = on_confirm
        text = Gtk.Label(label=body, wrap=True, xalign=0, max_width_chars=44)
        cancel = Gtk.Button(label=_("Cancel"))
        cancel.connect("clicked", weak_handler(self.cancel))
        self.accept = Gtk.Button(label=verb, css_classes=["destructive-action"])
        self.accept.connect("clicked", weak_handler(self.confirm))
        buttons = Gtk.Box(spacing=12, halign=Gtk.Align.END, homogeneous=True)
        buttons.append(cancel)
        buttons.append(self.accept)
        box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=18, margin_top=18, margin_bottom=18,
                      margin_start=18, margin_end=18)
        box.append(text)
        box.append(buttons)
        self.set_child(box)
        keys = Gtk.ShortcutController()
        keys.add_shortcut(Gtk.Shortcut(trigger=Gtk.ShortcutTrigger.parse_string("Escape"),
                                       action=Gtk.NamedAction.new("window.close")))
        self.add_controller(keys)
        cancel.grab_focus()  # the safe answer is the default one

    def confirm(self, *_args) -> None:
        done, self._on_confirm = self._on_confirm, None
        self.close()
        if done is not None:
            done()

    def cancel(self, *_args) -> None:
        self._on_confirm = None
        self.close()


class HomePage(Gtk.Box):
    """Recent files with thumbnails; ``on_open(path)`` is called for a card the user opens."""

    __gtype_name__ = "SovanderHomePage"

    def __init__(self, store: Store, on_open: Callable[[Path], None]):
        super().__init__(orientation=Gtk.Orientation.VERTICAL)
        Adw.init()
        self._store, self._on_open = store, on_open
        self._thumbs = thumbs.for_store(store)
        self._cards: dict[str, _Card] = {}
        self.confirm_dialog: _Confirm | None = None
        self._pruned = False

        self._title = Gtk.Label(xalign=0, hexpand=True, css_classes=["title-2"])
        self._keep_label = Gtk.Label()
        self.keep_switch = Gtk.Switch(valign=Gtk.Align.CENTER)
        self._clear = Gtk.Button(action_name="home.clear")
        bar = Gtk.Box(spacing=12, margin_top=12, margin_bottom=6, margin_start=18, margin_end=18)
        for part in (self._title, self._keep_label, self.keep_switch, self._clear):
            bar.append(part)
        self.append(bar)

        self.flow = Gtk.FlowBox(
            selection_mode=Gtk.SelectionMode.NONE, homogeneous=True, valign=Gtk.Align.START,
            max_children_per_line=12, column_spacing=6, row_spacing=6, activate_on_single_click=True,
            margin_top=6, margin_bottom=12, margin_start=12, margin_end=12,
        )
        self.flow.connect("child-activated", weak_handler(self._on_child_activated))
        keys = Gtk.EventControllerKey()
        keys.connect("key-pressed", weak_handler(self._on_key))
        self.flow.add_controller(keys)

        self._open_button = Gtk.Button(action_name=OPEN_ACTION, halign=Gtk.Align.CENTER,
                                       css_classes=["pill", "suggested-action"])
        self._empty = Adw.StatusPage(icon_name="document-open-recent-symbolic", vexpand=True,
                                     child=self._open_button)
        self._off = Adw.StatusPage(icon_name="document-open-recent-symbolic", vexpand=True)
        self.stack = Gtk.Stack(vexpand=True)
        scroller = Gtk.ScrolledWindow(child=self.flow, hscrollbar_policy=Gtk.PolicyType.NEVER, vexpand=True)
        self.stack.add_named(scroller, "recents")
        self.stack.add_named(self._empty, "empty")
        self.stack.add_named(self._off, "off")
        self.append(self.stack)

        self._actions = Gio.SimpleActionGroup()
        for name, handler, parameter in (
            ("open", self._act_open, "s"),
            ("remove", self._act_remove, "s"),
            ("clear", self._act_clear, None),
        ):
            action = Gio.SimpleAction.new(name, GLib.VariantType.new(parameter) if parameter else None)
            action.connect("activate", weak_handler(handler))
            self._actions.add_action(action)
        self.insert_action_group("home", self._actions)

        self._keep_id = self.keep_switch.connect("notify::active", weak_handler(self._on_keep))
        self.connect("map", weak_handler(self._on_map))
        i18n.add_listener(self._relabel)
        self._relabel()

    # Public

    @property
    def cards(self) -> list[_Card]:
        return list(self._cards.values())

    def refresh(self) -> None:
        """Read the recents again: after a change elsewhere, and each time the page is shown."""
        settings = self._store.settings
        with self.keep_switch.handler_block(self._keep_id):
            self.keep_switch.set_active(settings.history_enabled)
        recents = self._store.recents()
        self._actions.lookup_action("clear").set_enabled(bool(recents))
        while (child := self.flow.get_first_child()) is not None:
            self.flow.remove(child)
        self._cards.clear()
        if not settings.history_enabled:
            self.stack.set_visible_child_name("off")
            return
        if not recents:
            self.stack.set_visible_child_name("empty")
            return
        for recent in recents:
            card = _Card(recent, self._menu_for(recent))
            self.flow.append(card)
            self._cards[recent.uri] = card
            png = self._thumbs.path(recent)
            if png is None or not card.show_thumbnail(png):
                self._thumbs.request(recent, weak_handler(self._on_thumbnail))
        if not self._pruned:
            self._pruned = True
            self._thumbs.prune()
        self.stack.set_visible_child_name("recents")

    def remove(self, uri: str) -> None:
        """Forget one entry, its position and its thumbnail (11.3)."""
        self._store.remove(uri)
        self.refresh()

    def ask_clear_history(self) -> _Confirm:
        """Ask first; Clear History forgets every entry, position and thumbnail (11.3)."""
        if self.confirm_dialog is not None:
            self.confirm_dialog.close()
        root = self.get_root()
        self.confirm_dialog = _Confirm(
            root if isinstance(root, Gtk.Window) else None,
            _("Clear History?"),
            _("Sovander will forget every recent file, where you stopped reading and every thumbnail. "
              "The files themselves are not touched."),
            _("Clear History"),
            weak_handler(self._clear_confirmed),
        )
        self.confirm_dialog.present()
        return self.confirm_dialog

    # Handlers

    def _menu_for(self, recent: Recent) -> Gio.Menu:
        menu = Gio.Menu()
        target = GLib.Variant.new_string(recent.uri)
        items = [(_("Remove from List"), "home.remove")]
        if not recent.missing and local_path(recent.uri) is not None:
            items.insert(0, (_("Open"), "home.open"))
        for label, action in items:
            item = Gio.MenuItem.new(label, None)
            item.set_action_and_target_value(action, target)
            menu.append_item(item)
        return menu

    def _open_card(self, card: _Card | None) -> None:
        if card is not None and card.path is not None:
            self._on_open(card.path)

    def _on_child_activated(self, _flow, child) -> None:
        self._open_card(child if isinstance(child, _Card) else None)

    def _act_open(self, _action, parameter) -> None:
        self._open_card(self._cards.get(parameter.get_string()))

    def _act_remove(self, _action, parameter) -> None:
        self.remove(parameter.get_string())

    def _act_clear(self, *_args) -> None:
        self.ask_clear_history()

    def _clear_confirmed(self) -> None:
        self._store.clear_history()
        self.refresh()

    def _on_key(self, _controller, keyval, _code, state) -> bool:
        shift_f10 = keyval == Gdk.KEY_F10 and state & Gdk.ModifierType.SHIFT_MASK
        if keyval != Gdk.KEY_Menu and not shift_f10:
            return False
        card = self.flow.get_focus_child()
        if isinstance(card, _Card):
            card.menu_button.popup()
            return True
        return False

    def _on_keep(self, switch, _pspec) -> None:
        self._store.update_settings(history_enabled=switch.get_active())
        self.refresh()

    def _on_thumbnail(self, uri: str, png: Path | None) -> None:
        card = self._cards.get(uri)
        if card is not None and png is not None:
            card.show_thumbnail(png)

    def _on_map(self, *_args) -> None:
        self.refresh()

    def _relabel(self, _language: str | None = None) -> None:
        self._title.set_label(_("Recent Files"))
        self._keep_label.set_label(_("Keep History"))
        self.keep_switch.set_tooltip_text(_("Keep History"))
        self._clear.set_label(_("Clear History"))
        self._open_button.set_label(_("Open…"))
        self._empty.set_title(_("No Recent Files"))
        self._empty.set_description(_("Files you open appear here. Open a file or drop one on this window."))
        self._off.set_title(_("History Is Off"))
        self._off.set_description(_("Sovander does not remember the files you open, where you stopped "
                                    "reading or their thumbnails. Turn on Keep History to start."))
        self.refresh()
