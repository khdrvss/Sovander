"""A Sovander window: header bar, find bar, offline banner, and one document or the home screen.

Requirements 1.2-1.4, 3.1-3.4, 5.4, 6.1, 9.7, 12.2-12.5. Every action is reachable from the keyboard:
the app sets accelerators (app.ACCELS) and everything else is in the main menu, which F10 opens.
Handlers hold the window weakly (views.weak_handler), so a closed window and its view are freed.
"""

from __future__ import annotations

import importlib.util
import os

import gi

gi.require_version("Adw", "1")
gi.require_version("Gdk", "4.0")
gi.require_version("Gtk", "4.0")
from gi.repository import Adw, Gdk, Gio, GLib, Gtk  # noqa: E402

from sovander import i18n, offline, router  # noqa: E402
from sovander.find import FindBar  # noqa: E402
from sovander.i18n import N_, _  # noqa: E402
from sovander.open_document import DocumentPage, Source  # noqa: E402
from sovander.router import Kind  # noqa: E402
from sovander.views import weak_handler  # noqa: E402
from sovander.views.image_view import ImageView  # noqa: E402
from sovander.views.page_view import PageView  # noqa: E402

TITLE = "Sovander"
# What the file chooser's "Supported Files" filter lets through (1.4); content still decides the view
SUPPORTED = sorted(set(router.TEXT_EXTENSIONS) | {
    "pdf", "docx", "docm", "dotx", "dotm", "doc", "dot", "odt", "ott", "fodt", "rtf",
    "xlsx", "xlsm", "xlsb", "xltx", "xls", "xlt", "ods", "ots", "fods", "csv", "tsv",
    "pptx", "ppsx", "pptm", "potx", "ppt", "pps", "pot", "odp", "otp", "fodp", "odg", "otg", "fodg",
    "epub", "md", "markdown", "mdown", "mkd",
    "jpg", "jpeg", "png", "webp", "gif", "bmp", "svg", "svgz", "tif", "tiff", "heic", "heif", "avif", "ico",
    "zip",
})
MENU = (
    ((N_("Open…"), "win.open"), (N_("Print…"), "win.print")),
    ((N_("Find"), "win.find"), (N_("Go to Page"), "win.goto")),
    ((N_("Zoom In"), "win.zoom-in"), (N_("Zoom Out"), "win.zoom-out"),
     (N_("Fit Width"), "win.fit-width"), (N_("Fit Page"), "win.fit-page")),
    ((N_("Night Mode"), "win.night"), (N_("Presentation Mode"), "win.present"),
     (N_("Full Screen"), "win.fullscreen")),
    ((N_("About Sovander"), "win.about"),),
)
TEXT_SCALE_STEP = 1.1


def _is_web(view) -> bool:
    return type(view).__name__ == "WebView"  # by name: importing the web view loads WebKit


def _available(module: str) -> bool:
    try:
        return importlib.util.find_spec(module) is not None
    except (ImportError, ValueError):
        return False


class SovanderWindow(Adw.ApplicationWindow):
    """One document per window (1.3); a window without one shows the home screen (11.1)."""

    __gtype_name__ = "SovanderWindow"

    def __init__(self, application, source: Source | None = None):
        super().__init__(application=application, default_width=1000, default_height=760, title=TITLE)
        self.page: DocumentPage | None = None
        self.home: Gtk.Widget | None = None
        self._view = None
        self._view_ids: list[tuple[object, int]] = []
        self._editing_page = False
        self._chooser = None
        header = self._build_header()
        self.banner, self.banner_label = self._build_banner(application.offline_status)
        self.find_bar = FindBar()
        self.find_bar.attach(self)  # Ctrl+F opens it, Escape closes it (6.1)
        self._slot = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, vexpand=True)
        self.toasts = Adw.ToastOverlay(child=self._slot, vexpand=True)
        body = Gtk.Box(orientation=Gtk.Orientation.VERTICAL)
        for child in (header, self.banner, self.find_bar, self.toasts):
            body.append(child)
        self.set_content(body)
        self._add_actions(application)
        self.drop_target = Gtk.DropTarget.new(Gdk.FileList, Gdk.DragAction.COPY)  # files dropped on it (1.2)
        self.drop_target.connect("drop", weak_handler(self._on_drop))
        self.add_controller(self.drop_target)
        keys = Gtk.EventControllerKey()
        keys.connect("key-pressed", weak_handler(self._on_key))
        self.add_controller(keys)
        self.connect("close-request", weak_handler(self._on_close_request))
        self.connect("notify::fullscreened", weak_handler(self._on_fullscreened))
        i18n.add_listener(self._relabel)
        self._relabel()
        if source is not None:
            self.load(source)
        else:
            self.show_home()

    # Building

    def _build_header(self) -> Gtk.Widget:
        self.title_widget = Adw.WindowTitle(title=TITLE)
        header = Adw.HeaderBar(title_widget=self.title_widget)
        self.open_button = Gtk.Button(icon_name="document-open-symbolic", action_name="win.open")
        self.goto_entry = Gtk.Entry(width_chars=4, max_width_chars=7, xalign=1.0,
                                    input_purpose=Gtk.InputPurpose.DIGITS)
        self.goto_entry.connect("activate", weak_handler(self._on_goto))
        focus = Gtk.EventControllerFocus()
        focus.connect("enter", weak_handler(self._on_goto_focus))
        focus.connect("leave", weak_handler(self._on_goto_focus))
        self.goto_entry.add_controller(focus)
        self.count_label = Gtk.Label()
        self.page_box = Gtk.Box(spacing=6, visible=False)
        self.page_box.append(self.goto_entry)
        self.page_box.append(self.count_label)
        header.pack_start(self.open_button)
        header.pack_start(self.page_box)
        self.menu_button = Gtk.MenuButton(icon_name="open-menu-symbolic", primary=True)
        self.find_button = Gtk.Button(icon_name="edit-find-symbolic", action_name="win.find")
        self.night_button = Gtk.ToggleButton(icon_name="weather-clear-night-symbolic",
                                             action_name="win.night")
        self.present_button = Gtk.Button(icon_name="x-office-presentation-symbolic",
                                         action_name="win.present", visible=False)
        self.zoom_in_button = Gtk.Button(icon_name="zoom-in-symbolic", action_name="win.zoom-in")
        self.zoom_out_button = Gtk.Button(icon_name="zoom-out-symbolic", action_name="win.zoom-out")
        for button in (self.menu_button, self.find_button, self.night_button, self.present_button,
                       self.zoom_in_button, self.zoom_out_button):
            header.pack_end(button)
        return header

    def _build_banner(self, status) -> tuple[Gtk.Revealer, Gtk.Label]:
        """Offline protection is off: say so until dismissed (9.7)."""
        label = Gtk.Label(xalign=0, wrap=True, hexpand=True)
        self._banner_status = status
        self.dismiss_button = Gtk.Button(icon_name="window-close-symbolic")
        self.dismiss_button.add_css_class("flat")
        self.dismiss_button.connect("clicked", weak_handler(self.dismiss_banner))
        bar = Gtk.ActionBar()
        bar.pack_start(Gtk.Image(icon_name="dialog-warning-symbolic"))
        bar.pack_start(label)
        bar.pack_end(self.dismiss_button)
        shown = status is not None and status is not offline.OfflineStatus.VERIFIED
        return Gtk.Revealer(child=bar, reveal_child=shown), label

    def _menu(self) -> Gio.Menu:
        menu = Gio.Menu()
        for section in MENU:
            part = Gio.Menu()
            for label, action in section:
                part.append(_(label), action)
            menu.append_section(None, part)
        # Each language is named in itself, so it can be found whatever the menu is shown in (14.4)
        languages = Gio.Menu()
        for label, code in ((_("System Language"), "system"), ("English", "en"), ("Русский", "ru")):
            languages.append(label, f"win.language::{code}")
        part = Gio.Menu()
        part.append_submenu(_("Language"), languages)
        menu.insert_section(len(MENU) - 1, None, part)
        return menu

    def _language_code(self) -> str:
        store = getattr(self.get_application(), "store", None)
        return (store.settings.language if store is not None else None) or "system"

    def _on_language(self, action, value) -> None:
        """Requirement 14.4: the chosen language is kept and every window switches at once."""
        code = value.get_string()
        action.set_state(value)
        language = None if code == "system" else code
        store = getattr(self.get_application(), "store", None)
        if store is not None:
            store.update_settings(language=language)
        i18n.set_language(language)

    def _add_actions(self, application) -> None:
        for name, method in (
            ("open", self.choose_files), ("print", self._print), ("about", self._about), ("find", self._find),
            ("goto", self._focus_goto), ("zoom-in", self._zoom_in), ("zoom-out", self._zoom_out),
            ("fit-width", self._fit_width), ("fit-page", self._fit_page), ("present", self.present_slides),
            ("close", self._close),
        ):
            action = Gio.SimpleAction.new(name, None)
            action.connect("activate", weak_handler(method))
            self.add_action(action)
        night = application.context.night if application.context is not None else False
        for name, method, state in (("night", self._on_night, night),
                                    ("fullscreen", self._on_fullscreen, False)):
            action = Gio.SimpleAction.new_stateful(name, None, GLib.Variant.new_boolean(state))
            action.connect("change-state", weak_handler(method))
            self.add_action(action)
        language = Gio.SimpleAction.new_stateful(
            "language", GLib.VariantType.new("s"), GLib.Variant.new_string(self._language_code()))
        language.connect("change-state", weak_handler(self._on_language))
        self.add_action(language)

    def _relabel(self, _language=None) -> None:
        self.menu_button.set_menu_model(self._menu())
        action = self.lookup_action("language")
        if action is not None:  # another window may have made the choice
            action.set_state(GLib.Variant.new_string(self._language_code()))
        for widget, text in (
            (self.open_button, N_("Open…")), (self.find_button, N_("Find")),
            (self.night_button, N_("Night Mode")), (self.present_button, N_("Presentation Mode")),
            (self.zoom_in_button, N_("Zoom In")), (self.zoom_out_button, N_("Zoom Out")),
            (self.goto_entry, N_("Go to Page")),
        ):
            widget.set_tooltip_text(_(text))
        self.dismiss_button.set_tooltip_text(_("Dismiss"))
        if self._banner_status is not None:
            self.banner_label.set_text(offline.message(self._banner_status))
        self._update_counter()

    # Content

    def load(self, source: Source) -> None:
        """Show ``source`` in this window."""
        app = self.get_application()
        self.page = DocumentPage(source, app.context, on_open_entry=weak_handler(self._open_entry))
        self.page.connect("view-changed", weak_handler(self._bind_view))
        self._set_body(self.page)
        self.set_title(source.name)
        self.title_widget.set_title(source.name)
        self.page.start()
        self._bind_view()

    def show_home(self) -> None:
        """Recent files (11.1); the plain Open page while the home screen is not installed."""
        try:
            from sovander.home import HomePage
        except ImportError:
            HomePage = None  # noqa: N806
        if HomePage is not None:
            self.home = HomePage(self.get_application().store, weak_handler(self.open_path))
        else:
            button = Gtk.Button(label=_("Open…"), action_name="win.open", halign=Gtk.Align.CENTER)
            button.add_css_class("pill")
            self.home = Adw.StatusPage(icon_name="document-open-symbolic", title=TITLE, child=button)
        self._set_body(self.home)
        self._bind_view()

    def _set_body(self, widget: Gtk.Widget) -> None:
        child = self._slot.get_first_child()
        if child is not None:
            self._slot.remove(child)
        widget.set_vexpand(True)
        self._slot.append(widget)

    def open_path(self, path) -> None:
        self.open_paths([path])

    def open_paths(self, paths) -> None:
        """Open files here if this window is empty, otherwise each in a window of its own (1.3)."""
        self.get_application().open_paths(paths, window=self)

    def _open_entry(self, name: str, data: bytes) -> None:
        self.get_application().open_sources([Source.from_bytes(name, data)])

    # The view's controls

    def _bind_view(self, *_args) -> None:
        for obj, handler in self._view_ids:
            obj.disconnect(handler)
        self._view_ids = []
        view = self._view = self.page.view if self.page is not None else None
        if isinstance(view, PageView):
            for prop in ("current-page", "page-count"):
                handler = view.connect(f"notify::{prop}", weak_handler(self._update_counter))
                self._view_ids.append((view, handler))
        self.find_bar.set_backend(self.page.find_backend() if self.page is not None else None)
        self._update_actions()
        self._update_counter()

    def _update_actions(self) -> None:
        view = self._view
        paged, image, web = isinstance(view, PageView), isinstance(view, ImageView), _is_web(view)
        slides = paged and self.page.kind is Kind.SLIDES
        enabled = {
            "zoom-in": paged or image or web, "zoom-out": paged or image or web, "fit-width": paged or image,
            "fit-page": paged or image, "goto": paged, "night": paged or web, "present": slides,
            "find": self.page is not None and self.page.find_backend() is not None,
            "print": view is not None and _available("sovander.print"), "about": _available("sovander.about"),
        }
        for name, on in enabled.items():
            self.lookup_action(name).set_enabled(on)
        self.page_box.set_visible(paged)
        self.present_button.set_visible(slides)

    def _update_counter(self, *_args) -> None:
        """The page counter, "3 / 20" (3.1, 5.1); the entry also takes a page to go to (3.2)."""
        view = self._view
        if not isinstance(view, PageView):
            return
        count = view.page_count
        current = min(view.current_page + 1, count)
        if not self._editing_page:
            self.goto_entry.set_text(str(current))
        self.count_label.set_text(f"/ {count}")
        if self.page.kind is Kind.SLIDES:
            tip = _("Slide {slide} of {total}").format(slide=current, total=count)
        else:
            tip = _("Page {page} of {total}").format(page=current, total=count)
        self.count_label.set_tooltip_text(tip)

    @property
    def page_counter(self) -> str:
        if not self.page_box.get_visible():
            return ""
        return f"{self.goto_entry.get_text()} {self.count_label.get_text()}"

    def _on_goto(self, *_args) -> None:
        view = self._view
        text = self.goto_entry.get_text().strip()
        if isinstance(view, PageView) and text.isdigit() and 1 <= int(text) <= view.page_count:
            view.go_to(int(text) - 1)
            view.grab_focus()
        self._editing_page = False
        self._update_counter()

    def _on_goto_focus(self, controller, *_args) -> None:
        self._editing_page = controller.get_property("contains-focus")
        if not self._editing_page:
            self._update_counter()

    def _focus_goto(self, *_args) -> None:
        self.goto_entry.grab_focus()
        self.goto_entry.select_region(0, -1)

    def _zoom_in(self, *_args) -> None:
        self._zoom(TEXT_SCALE_STEP)

    def _zoom_out(self, *_args) -> None:
        self._zoom(1 / TEXT_SCALE_STEP)

    def _zoom(self, factor: float) -> None:
        view = self._view
        if isinstance(view, PageView | ImageView):
            (view.zoom_in if factor > 1 else view.zoom_out)()
        elif _is_web(view):
            view.set_text_scale(view.text_scale * factor)

    def _fit_width(self, *_args) -> None:
        if isinstance(self._view, PageView):
            self._view.fit_width()
        elif isinstance(self._view, ImageView):
            self._view.fit_to_window()

    def _fit_page(self, *_args) -> None:
        if isinstance(self._view, PageView):
            self._view.fit_page()
        elif isinstance(self._view, ImageView):
            self._view.fit_to_window()

    def _find(self, *_args) -> None:
        self.find_bar.open()

    def present_slides(self, *_args) -> None:
        """Presentation mode, one slide at a time full screen (5.4); Escape returns here (5.5)."""
        if isinstance(self._view, PageView):
            from sovander.views.present import present

            present(self._view, application=self.get_application())

    def _print(self, *_args) -> None:
        """The system print dialog (12.5); a view that cannot be printed says so in a toast."""
        if self._view is not None and _available("sovander.print"):
            from sovander.print import NotPrintable, print_view

            try:
                print_view(self, self._view)
            except NotPrintable as exc:
                self.show_toast(str(exc))

    def show_toast(self, text: str) -> None:
        self.toasts.add_toast(Adw.Toast(title=GLib.markup_escape_text(text)))

    def _about(self, *_args) -> None:
        if _available("sovander.about"):
            from sovander.about import show_about

            show_about(self, self.get_application().offline_status)

    # Night mode and full screen (12.2, 12.3)

    def _on_night(self, action, value) -> None:
        action.set_state(value)
        on = value.get_boolean()
        self.apply_night(on)
        app = self.get_application()
        if app.store is not None and app.store.settings.night_mode != on:
            try:
                app.store.update_settings(night_mode=on)
            except OSError:
                pass  # the setting stays for this session
            app.set_night(on)

    def apply_night(self, on: bool) -> None:
        action = self.lookup_action("night")
        if action.get_state().get_boolean() != on:
            action.set_state(GLib.Variant.new_boolean(on))
        if isinstance(self._view, PageView):
            self._view.night = on
        elif _is_web(self._view):
            self._view.set_night(on)

    def _on_fullscreen(self, action, value) -> None:
        action.set_state(value)
        (self.fullscreen if value.get_boolean() else self.unfullscreen)()

    def _on_fullscreened(self, *_args) -> None:
        self.lookup_action("fullscreen").set_state(GLib.Variant.new_boolean(self.is_fullscreen()))

    @property
    def fullscreen_requested(self) -> bool:
        """F11's state; a desktop without a window manager never confirms full screen."""
        return self.lookup_action("fullscreen").get_state().get_boolean()

    def _on_key(self, _controller, keyval, _code, _state) -> bool:
        if keyval == Gdk.KEY_Escape and self.fullscreen_requested:
            self.lookup_action("fullscreen").change_state(GLib.Variant.new_boolean(False))
            return True
        return False

    # Files in

    def choose_files(self, *_args) -> None:
        """The system file chooser, filtered to supported formats (1.4)."""
        chooser = Gtk.FileChooserNative(title=_("Open"), transient_for=self, modal=True,
                                        action=Gtk.FileChooserAction.OPEN, select_multiple=True)
        supported = Gtk.FileFilter(name=_("Supported Files"))
        for extension in SUPPORTED:
            supported.add_suffix(extension)
        everything = Gtk.FileFilter(name=_("All Files"))
        everything.add_pattern("*")
        chooser.add_filter(supported)
        chooser.add_filter(everything)
        chooser.set_filter(supported)
        chooser.connect("response", weak_handler(self._on_chosen))
        self._chooser = chooser  # a native chooser nobody holds is collected while it is open
        chooser.show()

    def _on_chosen(self, chooser, response) -> None:
        if response == Gtk.ResponseType.ACCEPT:
            files = chooser.get_files()
            self.open_paths(_paths(files.get_item(i) for i in range(files.get_n_items())))
        chooser.destroy()
        self._chooser = None

    def _on_drop(self, _target, value, _x, _y) -> bool:
        paths = _paths(value.get_files() if hasattr(value, "get_files") else ())
        if paths:
            self.open_paths(paths)
        return bool(paths)

    def dismiss_banner(self, *_args) -> None:
        self.banner.set_reveal_child(False)

    def _on_close_request(self, *_args) -> bool:
        if self.page is not None:
            self.page.close()
        return False

    def _close(self, *_args) -> None:
        self.close()


def _paths(files) -> list[str]:
    return [path for path in (f.get_path() for f in files) if path and os.path.isabs(path)]
