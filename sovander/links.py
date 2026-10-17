"""Links in documents (Requirements 3.11, 9.3-9.5).

A link to another place in the document scrolls there. Anything else never opens by itself: it goes
to ``confirm_and_open``, a dialog that shows the address in a form that cannot hide a different site
(host in punycode, invisible and look-alike-prone characters escaped) with Open, Copy and Cancel.

Open hands the address to the desktop portal (org.freedesktop.portal.OpenURI) over D-Bus. The
portal is a separate session service, so the browser it starts is not a child of Sovander and does
not inherit Sovander's no-network AppArmor profile; xdg-open, Gtk.show_uri or g_app_info_launch would
start it as our child, offline. Only http, https and mailto can be opened; other schemes are Copy only.
"""

from __future__ import annotations

import re
import unicodedata
from collections.abc import Callable
from dataclasses import dataclass, replace

import gi

from sovander import pdf_names
from sovander.i18n import _
from sovander.views import weak_handler
from sovander.views.selection import keep_with

try:
    import pymupdf as fitz
except ImportError:  # PyMuPDF before 1.24 (Ubuntu 22.04 and 24.04) has only the old name
    import fitz

gi.require_version("Gdk", "4.0")
gi.require_version("Gtk", "4.0")
gi.require_version("Pango", "1.0")
from gi.repository import Gdk, Gio, GLib, GObject, Gtk, Pango  # noqa: E402

__all__ = ["Address", "LinkDialog", "Links", "Portal", "confirm_and_open", "describe", "link_target"]

OPENABLE = ("http", "https", "mailto")
SPECIAL = ("http", "https")  # schemes browsers parse with an authority, slashes or not
MAX_SHOWN = 8192  # characters; a longer address cannot be read before confirming, so it is Copy only
PORTAL_NAME = "org.freedesktop.portal.Desktop"
PORTAL_PATH = "/org/freedesktop/portal/desktop"
PORTAL_IFACE = "org.freedesktop.portal.OpenURI"
PORTAL_TIMEOUT_MS = 25000  # the portal may ask the user to pick an application first

_SCHEME = re.compile(r"[A-Za-z][A-Za-z0-9+.-]*:")
_ASCII_HOST = re.compile(r"[a-z0-9_-]+(\.[a-z0-9_-]+)*\.?")
_IPV6 = re.compile(r"\[[0-9a-f:.]+\]")
_HIDDEN = {"Cc", "Cf", "Cs", "Co", "Cn", "Zs", "Zl", "Zp"}  # controls, bidi and zero-width marks, spaces


# Addresses


@dataclass(frozen=True)
class Address:
    """How an address is shown. ``shown`` is also exactly what Open and Copy use."""

    original: str
    shown: str
    scheme: str = ""
    host: str = ""  # ASCII (punycode) host of a web address
    unicode_host: str = ""  # the host as written, when it has non-Latin letters
    userinfo: bool = False  # "user@" before the host, a common way to fake a familiar name
    openable: bool = False
    too_long: bool = False


def _visible(text: str) -> str:
    """``text`` with every invisible or space character percent-encoded, so nothing hides."""
    out = []
    for c in text:
        if unicodedata.category(c) in _HIDDEN:
            out.append("".join(f"%{b:02X}" for b in c.encode("utf-8", "surrogatepass")))
        else:
            out.append(c)
    return "".join(out)


def _ascii_host(host: str) -> tuple[str, bool]:
    """(host in punycode, valid); IDNA 2003 as in Python's codec, which is also what Open sends."""
    host = host.lower()
    if _IPV6.fullmatch(host):
        return host, True
    if not host.isascii():
        try:
            host = host.encode("idna").decode("ascii")
        except UnicodeError:
            return _visible(host).encode("ascii", "backslashreplace").decode("ascii"), False
    return _visible(host), bool(_ASCII_HOST.fullmatch(host))


def _describe_web(uri: str, scheme: str, rest: str) -> Address:
    # Browsers take any run of slashes and backslashes after http: as the start of the authority,
    # which ends at the first / \ ? or #, and the site is after its last @ (WHATWG URL standard).
    rest = rest.lstrip("/\\")
    end = min((i for i in (rest.find(c) for c in "/\\?#") if i >= 0), default=len(rest))
    authority, tail = rest[:end], rest[end:]
    userinfo, at, hostport = authority.rpartition("@")
    if hostport.startswith("["):
        host, _sep, port = hostport.partition("]")
        host, port = host + "]", port[1:] if port.startswith(":") else port
    else:
        host, _sep, port = hostport.partition(":")
    ascii_host, valid = _ascii_host(host)
    valid = valid and bool(host) and (port.isascii() and port.isdigit()) == bool(port)
    shown = f"{scheme}://" + (_visible(userinfo) + "@" if at else "") + ascii_host
    shown += (f":{port}" if port else "") + _visible(tail)
    unicode_host = host.lower() if not host.isascii() else ""
    return Address(uri, shown, scheme, ascii_host, unicode_host, bool(at), valid)


def _describe_mail(uri: str, rest: str) -> Address:
    to, query_mark, query = rest.partition("?")
    valid, hosts, unicode_hosts, addresses = True, [], [], []
    for item in to.split(","):
        local, at, domain = item.rpartition("@")
        if not at:
            addresses.append(_visible(item))
            continue
        ascii_domain, ok = _ascii_host(domain)
        valid = valid and ok
        hosts.append(ascii_domain)
        if not domain.isascii():
            unicode_hosts.append(domain.lower())
        addresses.append(f"{_visible(local)}@{ascii_domain}")
    shown = "mailto:" + ",".join(addresses) + (query_mark + _visible(query) if query_mark else "")
    return Address(uri, shown, "mailto", ", ".join(hosts), ", ".join(unicode_hosts), False, valid)


def describe(uri: str) -> Address:
    """The address as the link dialog shows it, and whether it may be opened."""
    # Browsers drop leading and trailing spaces and control characters
    stripped = uri.strip("".join(map(chr, range(0x21))))
    match = _SCHEME.match(stripped)
    scheme = match.group()[:-1].lower() if match else ""
    if scheme in SPECIAL:
        address = _describe_web(uri, scheme, stripped[match.end() :])
    elif scheme == "mailto":
        address = _describe_mail(uri, stripped[match.end() :])
    else:
        address = Address(uri, _visible(stripped), scheme)
    if len(address.shown) > MAX_SHOWN:
        return replace(address, shown=address.shown[:MAX_SHOWN] + "…", openable=False, too_long=True)
    return address


# Link targets


NAMED_ACTIONS = ("FirstPage", "LastPage", "NextPage", "PrevPage")


def link_target(link: dict, current: int = 0, count: int = 0, resolve: Callable | None = None):
    """Where a ``page.get_links()`` entry leads: ("page", index), ("uri", address) or None.

    ``resolve(name)`` turns a named destination into a page index (or -1).
    """
    kind, page = link.get("kind"), link.get("page", -1)
    if kind in (fitz.LINK_GOTO, fitz.LINK_NAMED) and isinstance(page, int) and page >= 0:
        return ("page", page) if page < count or not count else None
    if kind == fitz.LINK_NAMED:
        name = link.get("nameddest") or link.get("name") or ""
        if name in NAMED_ACTIONS:
            to = {"FirstPage": 0, "LastPage": count - 1, "NextPage": current + 1, "PrevPage": current - 1}
            return ("page", min(max(0, to[name]), max(0, count - 1)))
        target = resolve(name) if resolve and name else -1
        return ("page", target) if target >= 0 else None
    if kind == fitz.LINK_URI:
        uri = link.get("uri") or ""
        if uri.startswith("#"):
            target = resolve(uri[1:]) if resolve else -1
            return ("page", target) if target >= 0 else None
        return ("uri", uri) if uri else None
    if kind in (fitz.LINK_LAUNCH, fitz.LINK_GOTOR):
        target = link.get("file") or link.get("uri") or ""
        return ("uri", target) if target else None  # another file or a program: shown, Copy only
    return None



def _with_uri(doc, link: dict) -> dict:
    """PyMuPDF before 1.24 (Ubuntu's) reads a URI action of a scheme MuPDF does not know (javascript:,
    smb:) as a launch with no file; the address is then taken from the action itself."""
    if link.get("kind") != fitz.LINK_LAUNCH or link.get("file") or not link.get("xref"):
        return link
    try:
        kind, uri = doc.xref_get_key(link["xref"], "A/URI")
    except Exception:  # noqa: BLE001 - a damaged annotation is just no link
        return link
    return {**link, "kind": fitz.LINK_URI, "uri": uri} if kind == "string" and uri else link

def _resolver(doc, lock):
    """``resolve(name)`` for a document: a named destination or a #page=N fragment to a page index."""

    def resolve(name: str) -> int:
        if name.startswith("page="):
            number = name[5:].split("&")[0]
            return int(number) - 1 if number.isdigit() else -1
        fragment = name if name.startswith("nameddest=") else "nameddest=" + name
        with lock:
            try:
                found = doc.resolve_link("#" + fragment)
            except (AttributeError, RuntimeError, ValueError):
                return -1
        page = found[0] if found else -1
        if isinstance(page, tuple):  # (chapter, page) in some versions
            page = page[-1]
        if not isinstance(page, int) or page < 0:  # PyMuPDF 1.19 resolves no names: read the file's own
            with lock:
                page = pdf_names.page_of(doc, fragment.removeprefix("nameddest="))
        return page

    return resolve


class Links:
    """Follows links on a PageView: pointer cursor over a link, a click follows it.

    Internal targets scroll the view; every other address goes to ``on_external(address)``, which
    by default is ``confirm_and_open`` on the view's window.
    """

    def __init__(self, view, on_external: Callable[[str], None] | None = None):
        self.view = view
        self.on_external = on_external or self._confirm
        self._doc, self._cache, self._hovered = None, {}, (False, None)
        motion = Gtk.EventControllerMotion()
        motion.connect("motion", weak_handler(self._on_motion))
        motion.connect("leave", weak_handler(self._on_leave))
        click = Gtk.GestureClick(button=Gdk.BUTTON_PRIMARY)
        click.connect("released", weak_handler(self._on_released))
        view.add_controller(motion)
        view.add_controller(click)
        keep_with(view, self)

    def links(self, page: int) -> list[dict]:
        """``get_links()`` of ``page``, cached per document."""
        doc = self.view.document
        if doc is not self._doc:
            self._doc, self._cache = doc, {}
        if page not in self._cache:
            links = []
            if doc is not None and 0 <= page < self.view.page_count:
                with self.view.lock:
                    links = [] if doc.is_closed else [_with_uri(doc, link) for link in doc[page].get_links()]
            self._cache[page] = links
        return self._cache[page]

    def link_at(self, x: float, y: float) -> dict | None:
        """The link under a widget point."""
        hit = self.view.widget_point_to_page(x, y)
        if hit is None:
            return None
        page, point = hit
        for link in self.links(page):
            if fitz.Rect(link["from"]).contains(point):
                return link
        return None

    def target(self, link: dict):
        doc = self.view.document
        resolve = _resolver(doc, self.view.lock) if doc is not None else None
        return link_target(link, self.view.current_page, self.view.page_count, resolve)

    def follow(self, link: dict) -> bool:
        """Go where ``link`` leads; False when it leads nowhere."""
        target = self.target(link)
        if target is None:
            return False
        kind, value = target
        if kind == "page":
            self.view.go_to(value)
        else:
            self.on_external(value)
        return True

    def activate_at(self, x: float, y: float) -> bool:
        link = self.link_at(x, y)
        return link is not None and self.follow(link)

    def hover(self, x: float, y: float) -> None:
        """Pointer cursor over a link; an outside address also shows as a tooltip."""
        link = self.link_at(x, y) if x >= 0 else None
        target = self.target(link) if link else None
        tip = describe(target[1]).shown if target and target[0] == "uri" else None
        if (bool(target), tip) != self._hovered:
            self._hovered = (bool(target), tip)
            self.view.set_cursor_from_name("pointer" if target else None)
            self.view.set_tooltip_text(tip)

    def _confirm(self, uri):
        confirm_and_open(self.view.get_root(), uri)

    def _on_motion(self, _controller, x, y):
        self.hover(x, y)

    def _on_leave(self, _controller):
        self.hover(-1, -1)

    def _on_released(self, _gesture, n_press, x, y):
        if n_press == 1:
            self.activate_at(x, y)


# Handing an address to the browser


def _connect_session(done):
    """Make a proxy for the portal on the session bus; ``done(proxy, None)`` or ``done(None, reason)``."""
    flags = Gio.DBusProxyFlags.DO_NOT_LOAD_PROPERTIES | Gio.DBusProxyFlags.DO_NOT_CONNECT_SIGNALS

    def ready(_source, result, *_args):
        try:
            done(Gio.DBusProxy.new_for_bus_finish(result), None)
        except GLib.Error as exc:
            done(None, exc.message)

    Gio.DBusProxy.new_for_bus(
        Gio.BusType.SESSION, flags, None, PORTAL_NAME, PORTAL_PATH, PORTAL_IFACE, None, ready
    )


class Portal:
    """org.freedesktop.portal.OpenURI over D-Bus. ``connect`` makes the proxy (tests pass a fake)."""

    def __init__(self, connect: Callable | None = None):
        self._connect = connect or _connect_session
        self._proxy = None

    def _with_proxy(self, then):
        if self._proxy is not None:
            then(self._proxy, None)
            return

        def connected(proxy, error):
            if proxy is not None and proxy.get_name_owner() is None:
                proxy, error = None, error or "org.freedesktop.portal.Desktop is not running"
            self._proxy = proxy
            then(proxy, error)

        try:
            self._connect(connected)
        except GLib.Error as exc:
            then(None, exc.message)

    def check(self, done: Callable[[bool], None]) -> None:
        """``done(True)`` when the portal answers on the session bus."""
        self._with_proxy(lambda proxy, _error: done(proxy is not None))

    def open_uri(self, uri: str, done: Callable[[str | None], None]) -> None:
        """Ask the portal to open ``uri``; ``done(None)`` once it took it, else ``done(reason)``."""

        def finish(proxy, result, *_args):
            try:
                proxy.call_finish(result)
            except GLib.Error as exc:
                done(exc.message)
            else:
                done(None)

        def call(proxy, error):
            if proxy is None:
                done(error or "no portal")
                return
            params = GLib.Variant("(ssa{sv})", ("", uri, {}))
            proxy.call("OpenURI", params, Gio.DBusCallFlags.NONE, PORTAL_TIMEOUT_MS, None, finish)

        self._with_proxy(call)


class LinkDialog(Gtk.Window):
    """Shows an outside address and asks before anything opens (9.3-9.5).

    Emits ``finished(result)`` once, with "opened", "copied" or "cancelled", then closes.
    """

    __gtype_name__ = "SovanderLinkDialog"
    __gsignals__ = {"finished": (GObject.SignalFlags.RUN_FIRST, None, (str,))}

    def __init__(self, parent: Gtk.Window | None, uri: str, portal: Portal | None = None):
        super().__init__(modal=True, transient_for=parent, destroy_with_parent=True, resizable=False)
        self.address, self.portal, self.result = describe(uri), portal or Portal(), None
        self.set_title(_("Open this web address?"))
        box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=12)
        for side in ("start", "end", "top", "bottom"):
            getattr(box, f"set_margin_{side}")(18)
        self.heading = Gtk.Label(xalign=0, wrap=True, max_width_chars=60)
        self.heading.add_css_class("title-4")
        # Plain text, never markup: the address cannot inject formatting
        self.address_label = Gtk.Label(label=self.address.shown, xalign=0, wrap=True, selectable=True)
        self.address_label.set_wrap_mode(Pango.WrapMode.CHAR)
        self.address_label.set_max_width_chars(60)
        self.address_label.add_css_class("monospace")
        scroller = Gtk.ScrolledWindow(child=self.address_label, propagate_natural_height=True)
        scroller.set_policy(Gtk.PolicyType.NEVER, Gtk.PolicyType.AUTOMATIC)
        scroller.set_max_content_height(240)
        self.note = Gtk.Label(xalign=0, wrap=True, max_width_chars=60)
        self.cancel_button = Gtk.Button(label=_("Cancel"))
        self.copy_button = Gtk.Button(label=_("Copy Address"))
        self.open_button = Gtk.Button(label=_("Open in Browser"))
        self.open_button.add_css_class("suggested-action")
        self.cancel_button.connect("clicked", weak_handler(self.cancel))
        self.copy_button.connect("clicked", weak_handler(self.copy))
        self.open_button.connect("clicked", weak_handler(self.open))
        buttons = Gtk.Box(spacing=8, halign=Gtk.Align.END)
        for button in (self.cancel_button, self.copy_button, self.open_button):
            buttons.append(button)
        for child in (self.heading, scroller, self.note, buttons):
            box.append(child)
        self.set_child(box)
        escape, trigger = Gtk.ShortcutController(), Gtk.ShortcutTrigger.parse_string("Escape")
        escape.add_shortcut(Gtk.Shortcut.new(trigger, Gtk.CallbackAction.new(weak_handler(self._on_escape))))
        self.add_controller(escape)
        self.connect("close-request", weak_handler(self._on_close_request))
        self._reasons = self._notes()
        self._show(self.address.openable, None)
        self.cancel_button.grab_focus()
        if self.address.openable:
            self.portal.check(weak_handler(self._on_checked))

    def _notes(self) -> list[str]:
        a, notes = self.address, []
        if a.scheme in SPECIAL and a.host:
            notes.append(_("Site: {host}").format(host=a.host))
        if a.unicode_host:
            notes.append(
                _(
                    "The site name has non-Latin letters ({name}), so it is shown encoded. "
                    "Look-alike letters can imitate a familiar site."
                ).format(name=a.unicode_host)
            )
        if a.userinfo:
            notes.append(
                _("The text before “@” is not the site. This link goes to {host}.").format(host=a.host)
            )
        if a.too_long:
            notes.append(_("This address is too long to show in full, so it can only be copied."))
        elif not a.openable and a.scheme in OPENABLE:
            notes.append(_("This address is malformed, so it can only be copied."))
        elif not a.openable:
            notes.append(_("Only web and email addresses can be opened from a document."))
        return notes

    def _show(self, can_open: bool, reason: str | None):
        self.heading.set_text(_("Open this web address?") if can_open else _("Copy this address?"))
        self.open_button.set_visible(can_open)
        notes = self._reasons + ([reason] if reason else [])
        self.note.set_text("\n".join(notes))
        self.note.set_visible(bool(notes))

    def _on_checked(self, available: bool):
        if self.result is None and not available:
            self._show(
                False,
                _(
                    "The browser cannot be started from here: the desktop portal is not "
                    "available. Copy the address and paste it into your browser."
                ),
            )

    def open(self, *_args) -> None:
        """Hand the shown address to the portal; on failure stay open with the reason and Copy."""
        if self.result is not None or not self.open_button.get_visible():
            return
        for button in (self.open_button, self.copy_button):
            button.set_sensitive(False)
        self.portal.open_uri(self.address.shown, weak_handler(self._on_opened))

    def _on_opened(self, error: str | None):
        if self.result is not None:
            return
        if error is None:
            self._finish("opened")
            return
        self.copy_button.set_sensitive(True)
        self.open_button.set_sensitive(True)
        self._show(
            False,
            _(
                "The browser could not be started ({reason}). "
                "Copy the address and paste it into your browser."
            ).format(reason=error),
        )

    def copy(self, *_args) -> None:
        self.get_clipboard().set(self.address.shown)
        self._finish("copied")

    def cancel(self, *_args) -> None:
        self._finish("cancelled")

    def _on_escape(self, *_args):
        self.cancel()
        return True

    def _finish(self, result: str):
        if self.result is None:
            self.result = result
            self.emit("finished", result)
            self.close()

    def _on_close_request(self, _window):
        if self.result is None:
            self.result = "cancelled"
            self.emit("finished", "cancelled")
        return False


def confirm_and_open(parent_window: Gtk.Window | None, uri: str, portal: Portal | None = None) -> LinkDialog:
    """Show the address of an outside link and ask before opening it (Requirements 9.3-9.5)."""
    dialog = LinkDialog(parent_window if isinstance(parent_window, Gtk.Window) else None, uri, portal)
    dialog.present()
    return dialog
