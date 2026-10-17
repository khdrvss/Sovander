"""
Spike copy of gander/tests/viewer/server.py (Gander repo is read-only).
Changes: VIEWER points at the Gander checkout, FIXTURES at the spike corpus,
and url() adds night=0 as ViewerActivity does. Everything else is verbatim.

A stand-in for the Android side of the viewer.

ViewerActivity serves the open document to the WebView out of
shouldInterceptRequest, and serves everything else from the APK's assets
through WebViewAssetLoader. This does the same two things over HTTP so the
shipped pages run unmodified: same paths, same query parameters, same range
semantics.

The range behaviour mirrors ViewerActivity.docResponse and DocRange.kt. The
one deliberate difference is that whether ranges are offered is set per test
rather than derived from the file size: the 16 MiB threshold itself is pinned
in DocRangeTest, and forcing a small fixture down the ranged path is the only
way to exercise it here in under a second.
"""

import os
import threading
from dataclasses import dataclass, field
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

# a checkout of https://github.com/mokshablr/gander, beside this repository unless GANDER_DIR says;
# read-only, assets served from here
GANDER = Path(os.environ.get("GANDER_DIR", Path(__file__).resolve().parents[3] / "gander"))
VIEWER = (GANDER / "app/src/main/assets/viewer").resolve()
FIXTURES = Path(__file__).resolve().parents[1] / "corpus"

# Content types the pages depend on. .mjs above all: pdf.html imports
# lib/pdf.min.mjs as a module, and a module served as anything but JavaScript
# is refused by the browser with nothing in the page to say why.
CONTENT_TYPES = {
    ".html": "text/html; charset=utf-8",
    ".js": "text/javascript; charset=utf-8",
    ".mjs": "text/javascript; charset=utf-8",
    ".css": "text/css; charset=utf-8",
    ".json": "application/json",
    ".bcmap": "application/octet-stream",
    ".pdf": "application/pdf",
    ".png": "image/png",
    ".jpg": "image/jpeg",
    ".gif": "image/gif",
    ".svg": "image/svg+xml",
    ".wav": "audio/wav",
    ".md": "text/markdown",
    ".txt": "text/plain",
    ".csv": "text/csv",
    ".docx": "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
    ".xlsx": "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
    ".pptx": "application/vnd.openxmlformats-officedocument.presentationml.presentation",
}


def parse_range(header, total):
    """ViewerActivity.parseRange, to the letter. None means serve it whole."""
    if total <= 0:
        return None
    if "bytes=" not in header:
        return None
    spec = header.split("bytes=", 1)[1].split(",", 1)[0].strip()
    if not spec:
        return None
    first, _, last = spec.partition("-")
    try:
        start = int(first.strip())
    except ValueError:
        return None
    try:
        end = int(last.strip())
    except ValueError:
        end = total - 1
    if start < 0 or start > end or start >= total:
        return None
    return start, min(end, total - 1)


@dataclass
class Served:
    """What the page is currently being shown."""
    path: Path = None
    ranged: bool = False
    status_override: int = 0
    requests: list = field(default_factory=list)


class ViewerServer:
    def __init__(self):
        self.state = Served()
        state = self.state

        class Handler(BaseHTTPRequestHandler):
            protocol_version = "HTTP/1.1"

            def log_message(self, *args):
                pass  # the test output is quiet unless something fails

            def _send(self, status, body, headers):
                # Recorded against the request this is answering, so a test can
                # tell "asked for and got it" from "asked for and got a 404".
                # Held on the handler rather than read back as the last entry in
                # the log: requests are answered on parallel threads, and pdf.js
                # asks for a CMap, a decoder and a range of the document at once,
                # so the last entry can belong to a request that came in after.
                record = getattr(self, "record", None)
                if record is not None:
                    record["status"] = status
                    record["bytes"] = len(body)
                self.send_response(status)
                for k, v in headers.items():
                    self.send_header(k, v)
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)

            def do_GET(self):
                path = self.path.split("?", 1)[0]
                self.record = {
                    "path": path,
                    "range": self.headers.get("Range"),
                    "status": None,
                    "bytes": 0,
                }
                state.requests.append(self.record)

                if path.startswith("/doc/"):
                    return self._document()
                if path.startswith("/assets/viewer/"):
                    return self._asset(path[len("/assets/viewer/"):])
                self._send(404, b"not found", {"Content-Type": "text/plain"})

            def _asset(self, relative):
                target = (VIEWER / relative).resolve()
                # The asset loader serves out of one directory and no further
                if not str(target).startswith(str(VIEWER)) or not target.is_file():
                    return self._send(404, b"no such asset", {"Content-Type": "text/plain"})
                ctype = CONTENT_TYPES.get(target.suffix, "application/octet-stream")
                self._send(200, target.read_bytes(), {"Content-Type": ctype})

            def _document(self):
                if state.status_override:
                    return self._send(
                        state.status_override, b"", {"Content-Type": "text/plain"}
                    )
                if state.path is None or not state.path.is_file():
                    return self._send(404, b"no document", {"Content-Type": "text/plain"})

                data = state.path.read_bytes()
                total = len(data)
                ctype = CONTENT_TYPES.get(state.path.suffix, "application/octet-stream")
                header = self.headers.get("Range")
                span = parse_range(header, total) if (state.ranged and header) else None

                if span is None:
                    headers = {"Content-Type": ctype}
                    if state.ranged:
                        headers["Accept-Ranges"] = "bytes"
                    return self._send(200, data, headers)

                start, end = span
                self._send(206, data[start:end + 1], {
                    "Content-Type": ctype,
                    "Accept-Ranges": "bytes",
                    "Content-Range": f"bytes {start}-{end}/{total}",
                })

        self.httpd = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self.port = self.httpd.server_address[1]
        self.thread = threading.Thread(target=self.httpd.serve_forever, daemon=True)
        self.thread.start()

    @property
    def origin(self):
        return f"http://127.0.0.1:{self.port}"

    def show(self, fixture, ranged=False, status=0):
        """Points /doc at a fixture, and says whether ranges are on offer."""
        self.state.path = fixture if isinstance(fixture, Path) else FIXTURES / fixture
        self.state.ranged = ranged
        self.state.status_override = status
        self.state.requests.clear()

    def url(self, page, name=None, ext=None, **params):
        """
        The URL ViewerActivity would load for this page.

        The length goes on it as ViewerActivity puts it there, whenever the file has one
        and it is not 0. Pass length=None for a file whose provider would not say, as one
        read out of a zip through a pipe will not.
        """
        from urllib.parse import quote
        if ext is None and self.state.path is not None:
            ext = self.state.path.suffix.lstrip(".")
        if name is None and self.state.path is not None:
            name = self.state.path.name
        query = [f"name={quote(name or 'file')}", f"ext={quote(ext or '')}"]
        query.append(f"ranged={1 if self.state.ranged else 0}")
        query.append("night=0")
        path = self.state.path
        if "length" not in params and path is not None and path.is_file() and path.stat().st_size:
            query.append(f"length={path.stat().st_size}")
        query += [f"{k}={quote(str(v))}" for k, v in params.items() if v is not None]
        return f"{self.origin}/assets/viewer/{page}?" + "&".join(query)

    def ranged_requests(self):
        return [r for r in self.state.requests
                if r["path"].startswith("/doc/") and r["range"]]

    def full_requests(self):
        return [r for r in self.state.requests
                if r["path"].startswith("/doc/") and not r["range"]]

    def served(self, fragment):
        """Requests whose path contains [fragment] and which were answered."""
        return [r for r in self.state.requests
                if fragment in r["path"] and r["status"] == 200]

    def stop(self):
        self.httpd.shutdown()
        self.httpd.server_close()
