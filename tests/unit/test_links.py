"""Links: how outside addresses are shown, where links lead, the portal call (task 4.2; 3.11, 9.3-9.5)."""

import ast
import threading
from pathlib import Path

import pytest

from sovander import links
from sovander.links import MAX_SHOWN, Portal, describe, link_target

try:
    import pymupdf as fitz
except ImportError:
    import fitz

from gi.repository import GLib  # noqa: E402

# Showing addresses (9.3)


def test_plain_web_address_is_shown_as_is_and_can_open():
    a = describe("https://example.com/a?b=1#top")
    assert (a.shown, a.scheme, a.host, a.openable) == (
        "https://example.com/a?b=1#top",
        "https",
        "example.com",
        True,
    )


def test_scheme_and_host_are_lower_cased_but_the_path_is_kept():
    assert describe("HTTPS://Example.COM/Path").shown == "https://example.com/Path"


def test_look_alike_host_is_shown_in_punycode():
    fake = "https://аpple.com/login"  # Cyrillic "а"
    a = describe(fake)
    assert a.host == "xn--pple-43d.com" and a.shown == "https://xn--pple-43d.com/login"
    assert a.unicode_host == "аpple.com" and a.openable


def test_russian_host_is_shown_in_punycode():
    a = describe("http://пример.рф/страница")
    assert a.shown == "http://xn--e1afmkfd.xn--p1ai/страница" and a.unicode_host == "пример.рф"


def test_text_before_at_is_flagged_and_the_real_host_named():
    a = describe("https://bank.example.com@evil.example/login")
    assert a.userinfo and a.host == "evil.example" and a.openable


def test_backslash_ends_the_host_as_in_browsers():
    a = describe("https://evil.example\\@bank.example/")
    assert a.host == "evil.example" and not a.userinfo


@pytest.mark.parametrize("hidden", ["‮", "​", " ", " ", "\t", "\n", "\x00"])
def test_invisible_characters_are_escaped(hidden):
    a = describe(f"https://example.com/a{hidden}b")
    assert hidden not in a.shown and a.shown.startswith("https://example.com/a%")


@pytest.mark.parametrize(
    "uri",
    [
        "https://exa mple.com/",
        "https://example.com:abc/",
        "https://example.com:８０/",  # full-width digits
        "https://a..b/",
        "https://%65xample.com/",
        "https://",
        "https://" + "я" * 70 + ".com/",
    ],
)
def test_malformed_web_addresses_are_copy_only(uri):
    assert not describe(uri).openable


@pytest.mark.parametrize(
    ("uri", "shown"),
    [
        ("http:///example.com/x", "http://example.com/x"),
        ("https:example.com", "https://example.com"),
        ("  \x00https://example.com", "https://example.com"),
        ("https://example.com:8443/x", "https://example.com:8443/x"),
        ("http://[::1]:8080/", "http://[::1]:8080/"),
    ],
)
def test_addresses_are_read_the_way_browsers_read_them(uri, shown):
    a = describe(uri)
    assert a.shown == shown and a.openable


@pytest.mark.parametrize(
    "uri",
    [
        "javascript:alert(1)",
        "file:///etc/passwd",
        "data:text/html,<script>alert(1)</script>",
        "smb://server/share",
        "ftp://example.com/",
        "other.pdf",
        "calc.exe",
    ],
)
def test_only_web_and_mail_addresses_can_open(uri):
    a = describe(uri)
    assert not a.openable and a.shown


def test_mail_address_domain_in_punycode():
    a = describe("mailto:иван@пример.рф,anna@example.com?subject=Привет")
    assert a.shown == "mailto:иван@xn--e1afmkfd.xn--p1ai,anna@example.com?subject=Привет"
    assert a.openable and a.unicode_host == "пример.рф"


def test_overlong_address_is_cut_and_copy_only():
    a = describe("https://example.com/" + "a" * (2 * MAX_SHOWN))
    assert a.too_long and not a.openable and len(a.shown) == MAX_SHOWN + 1 and a.shown.endswith("…")


# Where links lead (3.11)


@pytest.mark.parametrize(
    ("link", "expected"),
    [
        ({"kind": fitz.LINK_GOTO, "page": 3}, ("page", 3)),
        ({"kind": fitz.LINK_GOTO, "page": 30}, None),
        ({"kind": fitz.LINK_GOTO, "page": -1}, None),
        ({"kind": fitz.LINK_NAMED, "page": 2, "nameddest": "chap2"}, ("page", 2)),
        ({"kind": fitz.LINK_NAMED, "name": "LastPage"}, ("page", 9)),
        ({"kind": fitz.LINK_NAMED, "name": "FirstPage"}, ("page", 0)),
        ({"kind": fitz.LINK_NAMED, "name": "NextPage"}, ("page", 5)),
        ({"kind": fitz.LINK_NAMED, "name": "PrevPage"}, ("page", 3)),
        ({"kind": fitz.LINK_NAMED, "nameddest": "known"}, ("page", 7)),
        ({"kind": fitz.LINK_NAMED, "nameddest": "unknown"}, None),
        ({"kind": fitz.LINK_URI, "uri": "https://example.com"}, ("uri", "https://example.com")),
        ({"kind": fitz.LINK_URI, "uri": "#known"}, ("page", 7)),
        ({"kind": fitz.LINK_URI, "uri": ""}, None),
        ({"kind": fitz.LINK_LAUNCH, "file": "calc.exe", "page": 0}, ("uri", "calc.exe")),
        ({"kind": fitz.LINK_GOTOR, "file": "other.pdf", "page": 0}, ("uri", "other.pdf")),
        ({"kind": fitz.LINK_NONE}, None),
    ],
)
def test_link_targets(link, expected):
    def resolve(name):
        return 7 if name == "known" else -1

    assert link_target(link, current=4, count=10, resolve=resolve) == expected


def named_destination_pdf():
    doc = fitz.open()
    for i in range(4):
        doc.new_page().insert_text((72, 72), f"page {i + 1}")
    names = doc.get_new_xref()
    doc.update_object(names, f"<< /Names [(chapter2) [{doc[2].xref} 0 R /XYZ 0 700 0]] >>")
    doc.xref_set_key(doc.pdf_catalog(), "Names", f"<< /Dests {names} 0 R >>")
    return fitz.open(stream=doc.tobytes(), filetype="pdf")


def test_named_destinations_and_page_fragments_resolve():
    resolve = links._resolver(named_destination_pdf(), threading.RLock())
    assert resolve("chapter2") == 2
    assert resolve("nameddest=chapter2") == 2
    assert resolve("page=4") == 3
    assert resolve("nowhere") == -1


# The portal (9.4)


class FakeProxy:
    def __init__(self, owner=":1.42", error=None):
        self.owner, self.error, self.calls = owner, error, []

    def get_name_owner(self):
        return self.owner

    def call(self, method, params, flags, timeout, cancellable, callback, *user_data):
        self.calls.append((method, params.get_type_string(), params.unpack()))
        callback(self, object(), *user_data)

    def call_finish(self, _result):
        if self.error:
            raise GLib.Error(self.error)


def portal_with(proxy=None, error=None):
    made = []

    def connect(done):
        made.append(1)
        done(proxy, error)

    return Portal(connect), made


def test_open_hands_the_address_to_the_portal():
    proxy = FakeProxy()
    portal, made = portal_with(proxy)
    results = []
    portal.check(results.append)
    portal.open_uri("https://example.com/", results.append)
    assert results == [True, None]
    assert proxy.calls == [("OpenURI", "(ssa{sv})", ("", "https://example.com/", {}))]
    assert made == [1]  # one proxy for the check and the call


def test_portal_not_running():
    portal, _made = portal_with(FakeProxy(owner=None))
    results = []
    portal.check(results.append)
    portal.open_uri("https://example.com/", results.append)
    assert results[0] is False and isinstance(results[1], str) and results[1]


def test_no_session_bus():
    portal, _made = portal_with(None, "Cannot autolaunch D-Bus without X11 $DISPLAY")
    results = []
    portal.check(results.append)
    portal.open_uri("https://example.com/", results.append)
    assert results == [False, "Cannot autolaunch D-Bus without X11 $DISPLAY"]


def test_portal_refusal_is_reported():
    proxy = FakeProxy(error="Access denied")
    portal, _made = portal_with(proxy)
    results = []
    portal.open_uri("https://example.com/", results.append)
    assert len(proxy.calls) == 1 and "Access denied" in results[0]


def test_links_never_start_programs_themselves():
    """The browser must start outside our confinement: no spawning, no GTK/GIO launchers (9.4, 9.7)."""
    tree = ast.parse(Path(links.__file__).read_text(encoding="utf-8"))
    names = {n.id for n in ast.walk(tree) if isinstance(n, ast.Name)}
    names |= {n.attr for n in ast.walk(tree) if isinstance(n, ast.Attribute)}
    imported = {a.name.split(".")[0] for n in ast.walk(tree) if isinstance(n, ast.Import) for a in n.names}
    imported |= {n.module.split(".")[0] for n in ast.walk(tree) if isinstance(n, ast.ImportFrom) and n.module}
    forbidden = {"show_uri", "show_uri_full", "launch_default_for_uri", "launch_default_for_uri_async"}
    forbidden |= {"UriLauncher", "spawn_async", "spawn_command_line_async", "Popen", "system", "execvp"}
    assert not names & forbidden
    assert not imported & {"subprocess", "os", "webbrowser"}
