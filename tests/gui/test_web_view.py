import io
import json
import socket
import threading
import zipfile
from types import SimpleNamespace

import pytest

gi = pytest.importorskip("gi")
gi.require_version("WebKit", "6.0")
from gi.repository import GLib, WebKit  # noqa: E402

from sovander.views import web_view  # noqa: E402
from sovander.views.web_view import CSP, WebView, is_web_address  # noqa: E402

pytestmark = pytest.mark.gui

SETTLE = 1.5  # seconds after ready for late loads (pictures, fonts) to have been tried


def js(view, wait, body):
    """Run ``body`` as an async function in the page; its return value, through JSON."""
    box = {}

    def done(web, result):
        try:
            box["value"] = web.call_async_javascript_function_finish(result)
        except GLib.Error as e:
            box["error"] = e.message

    view.web.call_async_javascript_function(body, -1, None, None, None, None, done)
    assert wait(lambda: box, 10), "page did not answer"
    assert "error" not in box, box.get("error")
    value = box["value"]
    return None if value.is_undefined() else json.loads(value.to_json(0))


@pytest.fixture
def opened(window, wait):
    """Opens a document in a fresh view: opened(kind, data) -> view with .events, after ready."""

    def make(kind, data, ready=True, before=None):
        view = WebView()
        if before:
            before(view)
        view.events = []
        for name in ("ready", "error", "link-activated", "found"):
            view.connect(name, lambda _v, *args, name=name: view.events.append((name, *args)))
        view.messages = []
        view._content.connect(
            "script-message-received::sovander", lambda _c, value: view.messages.append(value.to_string())
        )
        window.set_child(view)
        window.present()
        (view.load_epub if kind == "epub" else view.load_markdown)(data, "doc." + kind)
        if ready:
            assert wait(lambda: view.is_ready or any(e[0] == "error" for e in view.events), 20)
            assert view.is_ready, view.events
        return view

    return make


@pytest.fixture
def listener():
    """A web server on 127.0.0.1 that records every connection and answers none."""
    server = socket.socket()
    server.bind(("127.0.0.1", 0))
    server.listen(64)
    server.settimeout(0.1)
    hits, stop = [], threading.Event()

    def accept():
        while not stop.is_set():
            try:
                conn, _ = server.accept()
            except TimeoutError:
                continue
            except OSError:
                return
            with conn:
                conn.settimeout(0.5)
                try:
                    hits.append(conn.recv(200))
                except OSError:
                    hits.append(b"")

    thread = threading.Thread(target=accept, daemon=True)
    thread.start()
    yield SimpleNamespace(url=f"http://127.0.0.1:{server.getsockname()[1]}", hits=hits)
    stop.set()
    thread.join(2)
    server.close()


def hostile_markdown(url):
    return f"""# Hostile

![remote]({url}/md-image.png)

<img src="{url}/raw-img.png" alt="raw"> <img src="x" onerror="window.__xss = 'onerror'">
<script>window.__xss = 'script'</script><script src="{url}/remote.js"></script>
<svg><image href="{url}/svg-image.png"/></svg><svg onload="window.__xss = 'svg'"></svg>
<iframe src="{url}/frame.html"></iframe><iframe srcdoc="<script>parent.__xss = 'srcdoc'</script>"></iframe>
<object data="{url}/object"></object><embed src="{url}/embed">
<video src="{url}/video.mp4" poster="{url}/poster.png"></video><audio src="{url}/audio.mp3" autoplay></audio>
<link rel="stylesheet" href="{url}/style.css"><link rel="preconnect" href="{url}/">
<link rel="prefetch" href="{url}/prefetch"><meta http-equiv="refresh" content="0; url={url}/refresh">
<base href="{url}/">
<style>@import url("{url}/import.css"); @font-face {{ font-family: x; src: url("{url}/font.woff"); }}
body {{ background: url("{url}/background.png"); font-family: x; }}</style>
<p style="background-image: url('{url}/inline-style.png')">styled</p>
<a href="{url}/ping-target" ping="{url}/ping">pinged link</a>
<form action="{url}/form"><input type="image" src="{url}/input.png"></form>
<details open ontoggle="window.__xss = 'toggle'"><summary>s</summary>d</details>

Text after the injected markup.
"""


def build_epub(url):
    """A one-chapter book whose chapter and stylesheet ask for things from ``url``."""
    chapter = f"""<?xml version="1.0" encoding="UTF-8"?>
<html xmlns="http://www.w3.org/1999/xhtml"><head><title>Hostile</title>
<link rel="stylesheet" href="style.css"/><link rel="stylesheet" href="{url}/epub-remote.css"/></head>
<body><h1>Hostile chapter</h1>
<p><img id="remote" src="{url}/epub-img.png" alt="Remote picture"/></p>
<p><img src="//{url[7:]}/protocol-relative.png" alt="pr"/></p>
<p style="background: url({url}/epub-inline.png)">styled</p>
<svg xmlns="http://www.w3.org/2000/svg" width="10" height="10">
<image href="{url}/epub-svg.png" width="10" height="10"/></svg>
<script src="{url}/epub.js"></script><script>window.__xss = 'script'</script>
<p><a id="web" href="https://example.org/book">a web address</a></p>
</body></html>"""
    style = f"""@import url("{url}/epub-import.css");
@font-face {{ font-family: far; src: url("{url}/epub-font.woff"); }}
body {{ font-family: far; background: url("{url}/epub-background.png"); }}"""
    package = """<?xml version="1.0" encoding="UTF-8"?>
<package xmlns="http://www.idpf.org/2007/opf" version="3.0" unique-identifier="id">
<metadata xmlns:dc="http://purl.org/dc/elements/1.1/"><dc:identifier id="id">urn:uuid:1</dc:identifier>
<dc:title>Hostile</dc:title><dc:language>en</dc:language></metadata>
<manifest><item id="c" href="chapter.xhtml" media-type="application/xhtml+xml"/>
<item id="s" href="style.css" media-type="text/css"/></manifest>
<spine><itemref idref="c"/></spine></package>"""
    container = """<?xml version="1.0"?>
<container version="1.0" xmlns="urn:oasis:names:tc:opendocument:xmlns:container"><rootfiles>
<rootfile full-path="package.opf" media-type="application/oebps-package+xml"/></rootfiles></container>"""
    out = io.BytesIO()
    with zipfile.ZipFile(out, "w") as z:
        z.writestr("mimetype", "application/epub+zip", compress_type=zipfile.ZIP_STORED)
        z.writestr("META-INF/container.xml", container)
        z.writestr("package.opf", package)
        z.writestr("chapter.xhtml", chapter)
        z.writestr("style.css", style)
    return out.getvalue()


def watch_loads(view):
    loads = []
    view.web.connect("resource-load-started", lambda _w, _r, request: loads.append(request.get_uri()))
    return loads


# Offline (9.1, 9.2): nothing a document names is asked of any host


def test_the_listener_hears_an_unguarded_webkit_view(window, wait, listener):
    """Control: a plain WebKit view here does reach the listener, so the silence below means something."""
    plain = WebKit.WebView(network_session=WebKit.NetworkSession.new_ephemeral())
    window.set_child(plain)
    window.present()
    plain.load_html(hostile_markdown(listener.url), listener.url + "/")
    assert wait(lambda: listener.hits, 15), "control failed: the listener heard nothing"


@pytest.mark.parametrize("kind", ["md", "epub"])
def test_remote_resources_are_never_requested(opened, wait, listener, kind):
    data = hostile_markdown(listener.url) if kind == "md" else build_epub(listener.url)
    view = opened(kind, data, ready=False)
    loads = watch_loads(view)
    assert wait(lambda: view.is_ready, 20), view.events
    wait(lambda: False, SETTLE)
    assert listener.hits == []
    assert loads and all(u.split(":", 1)[0] in ("sovander-asset", "data", "blob") for u in loads), loads
    # In their place the reader sees a placeholder (9.2)
    shown = js(
        view,
        wait,
        """
        const root = document.getElementById('book')?.shadowRoot || document;
        return [...root.querySelectorAll('.vw-remote')].map(e => [e.textContent, e.title]);""",
    )
    assert [
        "Remote picture" if kind == "epub" else "remote",
        "Remote image not loaded: " + listener.url + ("/epub-img.png" if kind == "epub" else "/md-image.png"),
    ] in shown


# No script from a document runs (10.1)


def test_markdown_scripts_and_handlers_never_run(opened, wait, listener, fixture_file):
    view = opened("md", hostile_markdown(listener.url) + fixture_file("notes.md").read_text())
    wait(lambda: False, 0.5)
    state = js(
        view,
        wait,
        """
        const c = document.getElementById('content');
        const handled = e => [...e.attributes].some(a => a.name.startsWith('on'));
        const scripts = c.querySelectorAll('script, iframe, object, embed').length;
        return {xss: window.__xss ?? null, scripts: scripts,
                handlers: [...c.querySelectorAll('*')].filter(handled).length,
                text: c.textContent.includes('Text after the injected markup')};""",
    )
    assert state == {"xss": None, "scripts": 0, "handlers": 0, "text": True}
    assert not [m for m in view.messages if "xss" in m]


def test_the_policy_refuses_inline_script_and_eval_even_past_the_sanitiser(opened, wait):
    view = opened("md", "# Policy")
    state = js(
        view,
        wait,
        """
        const s = document.createElement('script');
        s.textContent = "window.__inline = 'ran'";
        document.body.appendChild(s);
        let evaled = 'refused';
        try { evaled = eval('"ran"'); } catch (e) {}
        return [window.__inline ?? null, evaled];""",
    )
    assert state == [None, "refused"]


def test_epub_scripts_handlers_and_script_links_never_run(opened, wait, fixture_file):
    view = opened("epub", fixture_file("harbour.epub").read_bytes())
    view.next_chapter()
    assert wait(lambda: (view.position or "").startswith("1."), 10)
    state = js(
        view,
        wait,
        """
        const root = document.getElementById('book').shadowRoot;
        await new Promise(r => setTimeout(r, 300));
        for (const id of ['tapped', 'bad-link', 'missing']) root.getElementById(id)?.click();
        await new Promise(r => setTimeout(r, 300));
        return {xss: window.__xss ?? null, scripts: root.querySelectorAll('script').length,
                text: root.textContent.includes('which must survive it')};""",
    )
    assert state == {"xss": None, "scripts": 0, "text": True}


# Links (9.3): told to the app, never followed


def test_markdown_web_link_is_emitted_not_followed(opened, wait):
    view = opened(
        "md", "[out](https://example.org/a?b=1) [rel](other.md) [jump](#top)\n\n<h2 id=top>Top</h2>"
    )
    page = view.web.get_uri()
    js(view, wait, "for (const a of document.querySelectorAll('a')) a.click();")
    assert wait(lambda: any(e[0] == "link-activated" for e in view.events), 5)
    wait(lambda: False, 0.3)
    assert [e for e in view.events if e[0] == "link-activated"] == [
        ("link-activated", "https://example.org/a?b=1")
    ]
    assert view.web.get_uri().split("#")[0] == page


def test_epub_web_link_is_emitted_and_book_links_stay_inside(opened, wait, fixture_file):
    view = opened("epub", fixture_file("harbour.epub").read_bytes())
    view.next_chapter()
    assert wait(lambda: (view.position or "").startswith("1."), 10)
    js(view, wait, "document.getElementById('book').shadowRoot.getElementById('web-link').click();")
    assert wait(lambda: ("link-activated", "https://example.invalid/") in view.events, 5)
    js(view, wait, "document.getElementById('book').shadowRoot.getElementById('to-tide').click();")
    assert wait(lambda: (view.position or "").startswith("3."), 10)


@pytest.mark.parametrize(
    "target", ["https://example.org/away", "file:///etc/passwd", "sovander-asset://viewer/epub.html?ext=epub"]
)
def test_navigation_away_from_the_page_is_refused(opened, wait, target):
    view = opened("md", "# Stay")
    page = view.web.get_uri()
    # WebKit counts a script the app runs as the reader's own act, so this stands for a click
    js(view, wait, f"location.href = {json.dumps(target)};")
    if target.startswith("file:"):
        # WebKit refuses a file: address from a page of another scheme before it asks anyone
        wait(lambda: False, 1.0)
        assert target not in view.refused
    else:
        assert wait(lambda: target in view.refused, 5)
    wait(lambda: False, 0.3)
    assert view.web.get_uri() == page
    asked = [e[1] for e in view.events if e[0] == "link-activated"]
    assert asked == ([target] if target.startswith("https:") else [])


# Books (2.6, 11.2)


def test_book_opens_with_contents_and_turns_chapters(opened, wait, fixture_file):
    view = opened("epub", fixture_file("harbour.epub").read_bytes())
    assert wait(lambda: view.position == "0.0" and view.progress[1] > 0, 5)
    contents = js(
        view, wait, "return [...document.querySelectorAll('#epub-contents-list li')].map(l => l.textContent);"
    )
    assert "One: The Lamp Room" in contents and "Three: The Tide Table" in contents
    view.next_chapter()
    assert wait(lambda: (view.position or "").startswith("1."), 10)
    view.previous_chapter()
    assert wait(lambda: (view.position or "").startswith("0."), 10)
    view.show_contents()
    assert wait(lambda: js(view, wait, "return !document.getElementById('epub-contents').hidden;"), 5)


def test_reopens_at_the_saved_position(opened, wait, fixture_file):
    assert not WebView().restore_position("x") and not WebView().restore_position("1.2.3")
    # Before loading, as on reopening a recent book: the place goes to the page with the book
    view = opened(
        "epub", fixture_file("harbour.epub").read_bytes(), before=lambda v: v.restore_position("2.40")
    )
    assert "at=2.40" in view.web.get_uri()
    assert wait(lambda: (view.position or "").startswith("2."), 10)
    view.restore_position("3.0")
    assert wait(lambda: (view.position or "").startswith("3."), 10)


@pytest.mark.parametrize("name", ["locked.epub", None])
def test_a_book_that_cannot_be_read_reports_an_error(opened, wait, fixture_file, name):
    data = fixture_file(name).read_bytes() if name else b"not a zip at all"
    view = opened("epub", data, ready=False)
    assert wait(lambda: any(e[0] == "error" for e in view.events), 20)
    assert not view.is_ready


def test_a_renderer_crash_is_reported(opened, wait):
    view = opened("md", "# Crash")
    view.web.terminate_web_process()
    assert wait(lambda: any(e[0] == "error" for e in view.events), 10)


# Find (6)


def test_find_counts_moves_and_matches_case(opened, wait):
    view = opened("md", "Apple apple APPLE pie")

    def found(query, **kw):
        view.events.clear()
        view.find(query, **kw)
        assert wait(lambda: any(e[0] == "found" for e in view.events), 5)
        return [e for e in view.events if e[0] == "found"][-1][1:]

    assert found("apple") == (1, 3, True)
    assert found("apple") == (2, 3, True)
    assert found("apple", forward=False) == (1, 3, True)
    assert view.find("apple", forward=False) == (1, 3)  # the last answer, before the next arrives
    assert found("APPLE", case=True) == (1, 1, True)
    assert found("pear") == (0, 0, True)
    view.find_clear()
    assert view.find("") == (0, 0)


def test_find_searches_every_chapter_of_a_book(opened, wait, fixture_file):
    view = opened("epub", fixture_file("harbour.epub").read_bytes())

    def settled(query, **kw):
        view.events.clear()
        view.find(query, **kw)
        assert wait(lambda: any(e[0] == "found" and e[3] and e[2] for e in view.events), 10), view.events
        return [e for e in view.events if e[0] == "found"][-1][1:]

    assert settled("heronsgate") == (1, 3, True)  # chapter 1 once, chapter 3 twice
    assert (view.position or "").startswith("1.")
    assert settled("Heronsgate", case=True)[1] == 2


# Night and text size (12)


def test_night_turns_book_and_markdown_dark(opened, wait, fixture_file):
    view = opened("md", "# Night")
    ground = "return getComputedStyle(document.body).backgroundColor;"
    assert js(view, wait, ground) == "rgb(255, 255, 255)"
    view.set_night(True)
    assert wait(lambda: js(view, wait, ground) == "rgb(23, 19, 10)", 5)
    view.set_night(False)
    assert wait(lambda: js(view, wait, ground) == "rgb(255, 255, 255)", 5)

    view.set_night(True)
    view.load_epub(fixture_file("harbour.epub").read_bytes(), "harbour.epub")
    assert wait(lambda: view.is_ready, 20)
    ink = "return getComputedStyle(document.getElementById('book').shadowRoot.querySelector('h1')).color;"
    assert wait(lambda: js(view, wait, ink) == "rgb(231, 226, 217)", 5)


def test_text_scale_grows_the_text_within_limits(opened, wait):
    view = opened("md", "# Size\n\nA paragraph of text.")
    tall = "return document.querySelector('#content p').getBoundingClientRect().height;"
    before = js(view, wait, tall)
    view.set_text_scale(1.5)
    assert view.text_scale == pytest.approx(1.5)
    assert wait(lambda: js(view, wait, tall) > before * 1.3, 5)
    view.set_text_scale(100)
    assert view.text_scale == pytest.approx(4.0)
    view.set_text_scale(0.01)
    assert view.text_scale == pytest.approx(0.5)


# The scheme and the engine's settings


def test_the_scheme_serves_only_bundled_files_and_the_open_document(opened, wait):
    view = opened("md", "# Served")
    got = js(
        view,
        wait,
        """
        async function get(u) {
          try { const r = await fetch(u); return r.status + ' ' + r.headers.get('content-security-policy'); }
          catch (e) { return 'refused'; }
        }
        return Promise.all(['/doc/file.md', '/doc/file.epub', '/LICENSES.md', '/../sovander/paths.py',
          '/lib/marked.min.js', 'sovander-asset://elsewhere/app.js'].map(get));""",
    )
    assert got == [f"200 {CSP}", "refused", "refused", "refused", f"200 {CSP}", "refused"]


def test_settings_turn_off_what_the_pages_do_not_need():
    settings = WebView().web.get_settings()
    off = [
        "allow-file-access-from-file-urls",
        "allow-universal-access-from-file-urls",
        "enable-developer-extras",
        "enable-webgl",
        "enable-webaudio",
        "enable-media",
        "enable-webrtc",
        "enable-html5-local-storage",
        "javascript-can-open-windows-automatically",
        "javascript-can-access-clipboard",
        "enable-fullscreen",
    ]
    assert {name: settings.get_property(name) for name in off} == dict.fromkeys(off, False)
    assert settings.get_property("zoom-text-only")
    assert WebView().web.get_default_content_security_policy() == CSP


def test_context_menu_keeps_only_copying():
    view = WebView()
    menu = WebKit.ContextMenu.new()
    for action in (
        WebKit.ContextMenuAction.COPY,
        WebKit.ContextMenuAction.OPEN_LINK,
        WebKit.ContextMenuAction.DOWNLOAD_LINK_TO_DISK,
        WebKit.ContextMenuAction.RELOAD,
        WebKit.ContextMenuAction.INSPECT_ELEMENT,
    ):
        menu.append(WebKit.ContextMenuItem.new_from_stock_action(action))
    assert view._on_context_menu(None, menu, None) is False
    assert [i.get_stock_action() for i in menu.get_items()] == [WebKit.ContextMenuAction.COPY]
    only_reload = WebKit.ContextMenu.new()
    only_reload.append(WebKit.ContextMenuItem.new_from_stock_action(WebKit.ContextMenuAction.RELOAD))
    assert view._on_context_menu(None, only_reload, None) is True


@pytest.mark.parametrize(
    ("uri", "expected"),
    [
        ("https://example.org/", True),
        ("http://example.org/a b", True),
        ("mailto:a@example.org", True),
        ("tel:+100", True),
        ("javascript:alert(1)", False),
        ("file:///etc/passwd", False),
        ("sovander-asset://viewer/md.html", False),
        ("https://example.org/\x07", False),
        ("https://example.org/\u202egnp.exe", False),
        ("https://exa\u200bmple.org/", False),
        ("", False),
    ],
)
def test_is_web_address(uri, expected):
    assert is_web_address(uri) is expected


def test_assets_are_an_allowlist_of_page_files():
    files = web_view._asset_files()
    assert {"/epub.html", "/md.html", "/app.js", "/find.js", "/lib/purify.min.js"} <= set(files)
    assert all(path.endswith((".html", ".js", ".css")) for path in files)
    assert (web_view.ASSETS / "LICENSES.md").read_text().count("Apache License") >= 1
