"""No telemetry, crash reporting or update check can exist without network client code (9.1, 9.6).

The AppArmor profile stops such code at run time; this keeps it out of the source in the first
place. sovander/offline.py is the one module that may touch sockets: it creates and closes them to
prove they are refused, and never connects.
"""

import ast
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
PACKAGE = ROOT / "sovander"

# Modules whose only purpose here would be talking to another machine (or starting a browser
# inside the no-network profile instead of handing the link to the portal, 9.4)
FORBIDDEN_MODULES = {
    "aiohttp", "asyncio.streams", "ftplib", "http.client", "http.server", "httpx", "imaplib", "poplib",
    "requests", "sentry_sdk", "smtplib", "socketserver", "ssl", "telnetlib", "urllib.request",
    "urllib3", "webbrowser", "xmlrpc.client", "xmlrpc.server",
}
SOCKET_ALLOWED = {"sovander/offline.py"}
# Network clients reachable through GObject introspection
FORBIDDEN_GI = ("Soup.Session", "Gio.SocketClient", "Gio.NetworkAddress", "Gio.Resolver", "Gio.SocketService")


def imports(tree: ast.AST) -> set[str]:
    names: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            names.update(alias.name for alias in node.names)
        elif isinstance(node, ast.ImportFrom) and node.module and node.level == 0:
            names.add(node.module)
            names.update(f"{node.module}.{alias.name}" for alias in node.names)
    return names


def test_no_network_client_code_in_the_app():
    found = []
    for path in sorted(PACKAGE.rglob("*.py")):
        rel = path.relative_to(ROOT).as_posix()
        source = path.read_text(encoding="utf-8")
        for name in imports(ast.parse(source, rel)):
            if any(name == bad or name.startswith(bad + ".") for bad in FORBIDDEN_MODULES):
                found.append(f"{rel}: imports {name}")
            if name.split(".")[0] == "socket" and rel not in SOCKET_ALLOWED:
                found.append(f"{rel}: imports socket")
        found += [f"{rel}: uses {gi}" for gi in FORBIDDEN_GI if gi in source]
    assert not found, found
