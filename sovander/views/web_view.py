"""Web view (Requirements 2.6, 2.8, 9.1, 9.2, 10.1, 10.3, 11.2): EPUB books and Markdown in WebKitGTK.

Gander's pages in data/assets draw the document (see data/assets/LICENSES.md). Three walls keep a
document off the network and its code from running:

1. Every page, script and the document itself come from the ``sovander-asset:`` scheme, served
   from memory with ``CSP`` as a response header, so only Sovander's own scripts run and nothing
   loads from anywhere else. Books and Markdown pass through DOMPurify, as in Gander.
2. A WebKit content filter refuses every load whose address is not ``sovander-asset:``, ``data:``
   or ``blob:`` before any request is made, whatever a page's policy says. Until the filter is
   compiled nothing loads; if it cannot be compiled nothing ever does.
3. ``decide-policy`` refuses every navigation but to the page itself. A link to a web address is
   emitted as ``link-activated`` for the app to confirm (9.3); new windows and downloads are refused.

View and page talk in text: commands go to ``window.vwCommand`` (data/assets/find.js) and the page
answers through the script message handler ``sovander`` (``vwPost`` in data/assets/app.js).
"""

from __future__ import annotations

import json
import logging
import re
import unicodedata
import weakref
from collections import deque
from pathlib import Path
from urllib.parse import parse_qsl, urlencode, urlsplit

import gi

gi.require_version("Gdk", "4.0")
gi.require_version("Gtk", "4.0")
gi.require_version("Soup", "3.0")
gi.require_version("WebKit", "6.0")
from gi.repository import Gdk, Gio, GLib, GObject, Gtk, Soup, WebKit  # noqa: E402

from sovander import i18n, paths  # noqa: E402
from sovander.i18n import _  # noqa: E402
from sovander.views import weak_handler  # noqa: E402
from sovander.views.web_words import page_words  # noqa: E402

log = logging.getLogger(__name__)

ASSETS = Path(__file__).resolve().parents[2] / "data" / "assets"
SCHEME = "sovander-asset"
HOST = "viewer"
HANDLER = "sovander"

# Gander's ViewerPolicy.kt without 'wasm-unsafe-eval', worker-src and media-src, which only its PDF
# page and media used. 'unsafe-inline' styles stay: epub.js lays chapters out with style attributes.
CSP = (
    "default-src 'none'; script-src 'self'; style-src 'self' 'unsafe-inline'; "
    "img-src 'self' data: blob:; font-src 'self' data: blob:; connect-src 'self'; "
    "frame-src 'none'; object-src 'none'; form-action 'none'; base-uri 'none'"
)

# WebKit content-blocker rules: block everything, then let our own scheme and in-page data through.
# The regex dialect has no alternation, hence one rule per scheme.
FILTER_ID = "sovander-offline"
FILTER_RULES = json.dumps(
    [{"trigger": {"url-filter": ".*"}, "action": {"type": "block"}}]
    + [
        {"trigger": {"url-filter": f"^{scheme}:"}, "action": {"type": "ignore-previous-rules"}}
        for scheme in (SCHEME, "data", "blob")
    ]
)

_SETTINGS_OFF = """
    allow-file-access-from-file-urls allow-universal-access-from-file-urls allow-top-navigation-to-data-urls
    allow-modal-dialogs javascript-can-open-windows-automatically javascript-can-access-clipboard
    enable-developer-extras enable-write-console-messages-to-stdout enable-webgl enable-webaudio enable-media
    enable-media-stream enable-mediasource enable-media-capabilities enable-encrypted-media enable-webrtc
    enable-html5-database enable-html5-local-storage enable-offline-web-application-cache enable-page-cache
    enable-fullscreen enable-site-specific-quirks enable-back-forward-navigation-gestures
""".split()

PAGES = {"epub": "epub.html", "md": "md.html"}
DOC_TYPES = {"epub": "application/epub+zip", "md": "text/plain; charset=utf-8"}
ASSET_TYPES = {
    ".html": "text/html; charset=utf-8",
    ".js": "text/javascript; charset=utf-8",
    ".css": "text/css; charset=utf-8",
}
WEB_SCHEMES = {"http", "https", "mailto", "tel"}
POSITION = re.compile(r"^(\d{1,9})\.(\d{1,12})$")
TEXT_SCALE = (0.5, 4.0)

# Context menu entries that only copy; the rest open, download, reload or inspect
_MENU_KEEP = {
    WebKit.ContextMenuAction.COPY,
    WebKit.ContextMenuAction.SELECT_ALL,
    WebKit.ContextMenuAction.COPY_LINK_TO_CLIPBOARD,
    WebKit.ContextMenuAction.COPY_IMAGE_TO_CLIPBOARD,
}
_GROUND = {False: "#ffffff", True: "#17130A"}  # the pages' own paper, so no flash while loading


class _Shared:
    """One web context, network session and compiled filter for every view in the process."""

    context: WebKit.WebContext | None = None
    session: WebKit.NetworkSession | None = None
    filter: WebKit.UserContentFilter | None = None
    filter_failed = False
    waiting: list = []
    views: weakref.WeakValueDictionary = weakref.WeakValueDictionary()  # page id -> WebView
    assets: dict[str, Path] | None = None


def _engine() -> tuple[WebKit.WebContext, WebKit.NetworkSession]:
    if _Shared.context is None:
        context = WebKit.WebContext()
        context.set_cache_model(WebKit.CacheModel.DOCUMENT_VIEWER)
        context.register_uri_scheme(SCHEME, _serve)
        # Ephemeral: no cookies, cache or storage written anywhere (9.10)
        session = WebKit.NetworkSession.new_ephemeral()
        session.connect("download-started", lambda _session, download: download.cancel())
        _Shared.context, _Shared.session = context, session
    return _Shared.context, _Shared.session


def _when_filtered(done) -> None:
    """Call ``done(filter)`` once the content filter is compiled, or ``done(None)`` if it cannot be."""
    if _Shared.filter is not None or _Shared.filter_failed:
        done(_Shared.filter)
        return
    _Shared.waiting.append(done)
    if len(_Shared.waiting) > 1:
        return
    store = WebKit.UserContentFilterStore.new(str(paths.cache_dir() / "web-filter"))
    store.save(FILTER_ID, GLib.Bytes.new(FILTER_RULES.encode()), None, _filter_saved)


def _filter_saved(store, result) -> None:
    try:
        _Shared.filter = store.save_finish(result)
    except GLib.Error as e:
        log.error("web content filter not compiled: %s", e.message)
        _Shared.filter_failed = True
    waiting, _Shared.waiting = _Shared.waiting, []
    for done in waiting:
        done(_Shared.filter)


def _asset_files() -> dict[str, Path]:
    """The files the scheme serves, by path: an allowlist, so no path can reach outside ASSETS."""
    if _Shared.assets is None:
        _Shared.assets = {
            "/" + p.relative_to(ASSETS).as_posix(): p
            for p in ASSETS.rglob("*")
            if p.is_file() and p.suffix in ASSET_TYPES
        }
    return _Shared.assets


def _serve(request: WebKit.URISchemeRequest) -> None:
    found = None
    path = request.get_path() or ""
    if request.get_http_method() in (None, "GET") and urlsplit(request.get_uri()).hostname == HOST:
        if path.startswith("/doc/"):
            web = request.get_web_view()
            view = _Shared.views.get(web.get_page_id()) if web is not None else None
            found = view._document(path) if view is not None else None
        elif path in _asset_files():
            file = _asset_files()[path]
            found = (file.read_bytes(), ASSET_TYPES[file.suffix])
    if found is None:
        request.finish_error(
            GLib.Error.new_literal(Gio.io_error_quark(), "Not found", Gio.IOErrorEnum.NOT_FOUND)
        )
        return
    body, content_type = found
    response = WebKit.URISchemeResponse.new(
        Gio.MemoryInputStream.new_from_bytes(GLib.Bytes.new(body)), len(body)
    )
    headers = Soup.MessageHeaders.new(Soup.MessageHeadersType.RESPONSE)
    # set_http_headers() replaces every header, the type set_content_type() wrote among them, and
    # with nosniff a script of no type is refused: so the type goes in both, headers first
    headers.append("Content-Type", content_type)
    headers.append("Content-Security-Policy", CSP)
    headers.append("X-Content-Type-Options", "nosniff")
    headers.append("Cache-Control", "no-store")
    response.set_http_headers(headers)
    response.set_content_type(content_type)
    request.finish_with_response(response)


def _settings() -> WebKit.Settings:
    """Everything off that Gander's two pages do not need; text zoom reflows the text, not the page."""
    settings = WebKit.Settings(zoom_text_only=True)
    for name in _SETTINGS_OFF:
        settings.set_property(name, False)
    return settings


def is_web_address(uri: str) -> bool:
    """Whether ``uri`` is an address the app may offer to open: web, mail or phone, shown in full.

    Control and format characters are refused: a right-to-left override or a zero-width letter
    would make the address the reader confirms look like another (9.3).
    """
    if not uri or len(uri) > 8192 or any(unicodedata.category(c) in ("Cc", "Cf") for c in uri):
        return False
    try:
        return urlsplit(uri).scheme.lower() in WEB_SCHEMES
    except ValueError:
        return False


def _same_page(a: str, b: str) -> bool:
    """Equal but for the fragment, comparing the query by value: WebKit may respell its escapes."""
    try:
        x, y = ((u.scheme, u.netloc, u.path, parse_qsl(u.query)) for u in map(urlsplit, (a, b)))
    except ValueError:
        return False
    return x == y


def _words_script() -> WebKit.UserScript:
    """The pages' words in the app's language, set before their scripts run (14.1). The app's own
    script, not the page's: the policy still refuses every inline script the page could run."""
    return WebKit.UserScript.new(
        f"window.vwWords = {json.dumps(page_words())};", WebKit.UserContentInjectedFrames.TOP_FRAME,
        WebKit.UserScriptInjectionTime.START, [f"{SCHEME}://{HOST}/*"], None,
    )


class WebView(Gtk.Box):
    """An EPUB book or a Markdown file in Gander's pages, offline and with no script of its own."""

    __gtype_name__ = "SovanderWebView"
    __gsignals__ = {
        "ready": (GObject.SignalFlags.RUN_FIRST, None, ()),
        "error": (GObject.SignalFlags.RUN_FIRST, None, (str,)),
        "link-activated": (GObject.SignalFlags.RUN_FIRST, None, (str,)),
        "found": (GObject.SignalFlags.RUN_FIRST, None, (int, int, bool)),
    }

    def __init__(self):
        super().__init__(orientation=Gtk.Orientation.VERTICAL)
        context, session = _engine()
        self._content = WebKit.UserContentManager()
        self._content.connect(f"script-message-received::{HANDLER}", weak_handler(self._on_message))
        self._content.register_script_message_handler(HANDLER, None)
        self._content.add_script(_words_script())
        i18n.add_listener(self._relabel)
        self._filtered = False
        self.web = WebKit.WebView(
            web_context=context,
            network_session=session,
            user_content_manager=self._content,
            settings=_settings(),
            default_content_security_policy=CSP,
            hexpand=True,
            vexpand=True,
        )
        self.web.connect("decide-policy", weak_handler(self._on_decide_policy))
        self.web.connect("create", lambda *_args: None)
        self.web.connect("context-menu", weak_handler(self._on_context_menu))
        self.web.connect("permission-request", lambda _web, request: request.deny() or True)
        self.web.connect("web-process-terminated", weak_handler(self._on_terminated))
        self.web.connect("load-failed", weak_handler(self._on_load_failed))
        self.append(self.web)
        _Shared.views[self.web.get_page_id()] = self

        self.name = ""
        self.refused: deque[str] = deque(maxlen=50)  # navigations refused, newest last
        self._generation = 0
        self._doc: bytes | None = None
        self._kind: str | None = None
        self._page_uri = ""
        self._ready = self._failed = False
        self._pending: list[str] = []
        self._night = False
        self._restore: str | None = None
        self._position: str | None = None
        self._progress = (0, 0)
        self._query: str | None = None
        self._case = False
        self._found = (0, 0)
        self._set_ground()

    # Loading

    def load_markdown(self, text: str, name: str) -> None:
        """Show Markdown rendered as formatted text (2.8)."""
        self._load(text.encode("utf-8"), "md", name)

    def load_epub(self, data: bytes, name: str) -> None:
        """Show a book chapter by chapter with its contents (2.6); a book with DRM shows the page's error."""
        self._load(bytes(data), "epub", name)

    @property
    def is_ready(self) -> bool:
        return self._ready

    def _load(self, data: bytes, kind: str, name: str) -> None:
        self._generation += 1
        generation = self._generation
        self.name, self._doc, self._kind = name, data, kind
        self._ready, self._failed, self._pending = False, False, []
        self._position, self._progress = None, (0, 0)
        self._query, self._found = None, (0, 0)
        params = {"ext": kind}
        if self._night:
            params["night"] = "1"
        if kind == "epub" and self._restore:
            params["at"], self._restore = self._restore, None
        self._page_uri = f"{SCHEME}://{HOST}/{PAGES[kind]}?{urlencode(params)}"
        ref = weakref.ref(self)

        def start(content_filter):
            view = ref()
            if view is not None and view._generation == generation:
                view._start(content_filter)

        _when_filtered(start)

    def _start(self, content_filter) -> None:
        if content_filter is None:
            self.emit("error", _("Offline protection could not be checked"))
            return
        if not self._filtered:
            self._content.add_filter(content_filter)
            self._filtered = True
        self.web.load_uri(self._page_uri)

    def _document(self, path: str) -> tuple[bytes, str] | None:
        if self._doc is None or path != f"/doc/file.{self._kind}":
            return None
        return self._doc, DOC_TYPES[self._kind]

    # Reading

    @property
    def position(self) -> str | None:
        """Where the reader is in a book, "<chapter>.<letters before>", for restore_position (11.2)."""
        return self._position

    def restore_position(self, position: str) -> bool:
        """Go back to a place ``position`` gave; before a book is loaded it applies to the next one."""
        if not position or not POSITION.match(position):
            return False
        if self._kind == "epub" and self._page_uri:
            self._send("g" + position)
        else:
            self._restore = position
        return True

    @property
    def progress(self) -> tuple[int, int]:
        """How far through a book the screen is, (page, pages) in Gander's 2 KB pages; (0, 0) for Markdown."""
        return self._progress

    def show_contents(self) -> None:
        self._send("o")

    def next_chapter(self) -> None:
        self._send(">")

    def previous_chapter(self) -> None:
        self._send("<")

    def set_night(self, on: bool) -> None:
        """Light text on a dark page, pictures as they are (Gander's book night)."""
        self._night = bool(on)
        self._set_ground()
        if self._page_uri:
            self._send("N1" if self._night else "N0")

    def set_text_scale(self, scale: float) -> None:
        """Text size, 1.0 as the page sets it; the layout follows (zoom-text-only)."""
        self.web.set_zoom_level(min(max(float(scale), TEXT_SCALE[0]), TEXT_SCALE[1]))

    @property
    def text_scale(self) -> float:
        return self.web.get_zoom_level()

    # Find (6): Gander's find.js searches every chapter, not just the one in the DOM

    def find(self, query: str, forward: bool = True, case: bool = False) -> tuple[int, int]:
        """Search; repeating a query moves to the next or previous match.

        The page answers asynchronously with ``found(index, total, finished)``, index counting
        from 1 and 0 for none. This returns the last answer known, which for a new query is (0, 0).
        """
        query = query.replace("\0", "")
        if not query:
            self.find_clear()
            return (0, 0)
        if (query, bool(case)) != (self._query, self._case):
            self._query, self._case, self._found = query, bool(case), (0, 0)
            self._send(("Q" if case else "q") + query)
        else:
            self._send("n" if forward else "p")
        return self._found

    def find_clear(self) -> None:
        self._query, self._found = None, (0, 0)
        self._send("c")

    # Talking to the page

    def _send(self, command: str) -> None:
        if not self._ready and not self._failed:  # a page that failed still hears new words
            self._pending.append(command)
            return
        self.web.call_async_javascript_function(
            "if (window.vwCommand) vwCommand(m);",
            -1,
            GLib.Variant("a{sv}", {"m": GLib.Variant("s", command)}),
            None,
            None,
            None,
            _javascript_done,
        )

    def _on_message(self, _content, value) -> None:
        text = value.to_string() if value.is_string() else ""
        verb, _space, rest = text.partition(" ")
        numbers = [int(n) for n in text.split() if n.isdigit()] if verb.isdigit() else []
        if verb == "ready" and not self._ready:
            self._ready = True
            pending, self._pending = self._pending, []
            for command in pending:
                self._send(command)
            self.emit("ready")
        elif verb == "error":
            self._failed = True
            title, _newline, detail = rest.partition("\n")
            self.emit("error", f"{title}: {detail.strip()}" if detail.strip() else title)
        elif verb == "link" and is_web_address(rest):
            self.emit("link-activated", rest)
        elif verb == "page" and len(rest.split()) == 2 and rest.replace(" ", "").isdigit():
            self._progress = tuple(int(n) for n in rest.split())
        elif verb == "at" and POSITION.match(rest.replace(" ", ".", 1)):
            self._position = rest.replace(" ", ".", 1)
        elif len(numbers) == 3 and self._query is not None:
            self._found = (numbers[0], numbers[1])
            self.emit("found", numbers[0], numbers[1], bool(numbers[2]))

    def _relabel(self, _language=None) -> None:
        """The page's words follow the language (14.4): for the next page and the one shown."""
        self._content.remove_all_scripts()
        self._content.add_script(_words_script())
        if self._page_uri:
            self._send("W" + json.dumps(page_words()))

    # Guards

    def _on_decide_policy(self, _web, decision, kind) -> bool:
        if kind == WebKit.PolicyDecisionType.RESPONSE:
            return False
        action = decision.get_navigation_action()
        uri = action.get_request().get_uri() or ""
        if kind == WebKit.PolicyDecisionType.NAVIGATION_ACTION and _same_page(uri, self._page_uri):
            decision.use()
            return True
        decision.ignore()
        self.refused.append(uri)
        if action.is_user_gesture() and is_web_address(uri):
            self.emit("link-activated", uri)
        return True

    def _on_context_menu(self, _web, menu, _hit) -> bool:
        for item in list(menu.get_items()):
            if item.get_stock_action() not in _MENU_KEEP:
                menu.remove(item)
        return menu.get_n_items() == 0

    def _on_terminated(self, _web, reason) -> None:
        log.warning("web process ended: %s", reason.value_nick)
        self._ready = False
        self.emit("error", _("The document engine stopped while opening this file"))

    def _on_load_failed(self, _web, _event, uri, error) -> bool:
        cancelled = error.matches(WebKit.network_error_quark(), WebKit.NetworkError.CANCELLED) or (
            error.matches(
                WebKit.policy_error_quark(), WebKit.PolicyError.FRAME_LOAD_INTERRUPTED_BY_POLICY_CHANGE
            )
        )
        if not cancelled and _same_page(uri, self._page_uri):
            self.emit("error", error.message)
        return False

    def _set_ground(self) -> None:
        colour = Gdk.RGBA()
        colour.parse(_GROUND[self._night])
        self.web.set_background_color(colour)


def _javascript_done(web, result) -> None:
    try:
        web.call_async_javascript_function_finish(result)
    except GLib.Error as e:
        log.debug("page command failed: %s", e.message)
