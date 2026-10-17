"""Offline probe (Requirements 9.1, 9.7, 9.8): what can a process do on the network right now?

Run it as Sovander runs, to test the profile itself:
    aa-exec -p sovander -- /usr/bin/python3 -I -B tests/offline/probe.py --libdir /usr/lib/sovander
and unconfined, as the control that shows the probe can tell the difference. Prints one JSON object:

  status   sovander.offline.check() from LIBDIR: "verified", "unprotected" or "unknown"
  label    this process's AppArmor label
  inet     AF_INET stream socket: "denied:EACCES" (or another errno), "open", or "connected" when it
           opened and reached the canary port (only ever 127.0.0.1, the test's own listener)
  inet6    the same for AF_INET6 (creation only)
  unix     an AF_UNIX listener in a temporary folder, a connect and a round trip: "ok" or the error
  child    the inet result of a child process this one starts (children must inherit the profile)
  engine   with --engine-file: the LibreOffice engine started from LIBDIR converts the file over its
           UNO pipe, "ok: N bytes" or the error (the engine must work inside the profile)

stdlib only, plus sovander from LIBDIR. Never sends anything anywhere but the canary on loopback.
"""

from __future__ import annotations

import argparse
import errno
import json
import os
import socket
import subprocess
import sys
import tempfile
from pathlib import Path


def label() -> str | None:
    for attr in ("/proc/self/attr/apparmor/current", "/proc/self/attr/current"):
        try:
            text = Path(attr).read_text(encoding="utf-8", errors="replace").strip("\0\n ")
        except OSError:
            continue
        if text:
            return text
    return None


def inet(family: socket.AddressFamily, canary_port: int | None) -> str:
    try:
        sock = socket.socket(family, socket.SOCK_STREAM)
    except OSError as exc:
        return f"denied:{errno.errorcode.get(exc.errno, exc.errno)}"
    try:
        if canary_port is None or family != socket.AF_INET:
            return "open"
        sock.settimeout(5)
        try:
            sock.connect(("127.0.0.1", canary_port))
        except OSError as exc:
            return f"open, connect failed:{errno.errorcode.get(exc.errno, exc.errno)}"
        return "connected"
    finally:
        sock.close()


def unix_round_trip() -> str:
    with tempfile.TemporaryDirectory(prefix="sovander-probe-") as folder:
        path = os.path.join(folder, "s")
        try:
            with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as server:
                server.bind(path)
                server.listen(1)
                with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as client:
                    client.settimeout(5)
                    client.connect(path)
                    peer, _ = server.accept()
                    with peer:
                        client.sendall(b"ping")
                        if peer.recv(4) != b"ping":
                            return "error: wrong bytes"
        except OSError as exc:
            return f"error: {exc}"
    return "ok"


def child(canary_port: int | None) -> dict:
    cmd = [sys.executable, "-I", "-B", __file__, "--child-only"]
    if canary_port is not None:
        cmd += ["--canary-port", str(canary_port)]
    out = subprocess.run(cmd, capture_output=True, text=True, timeout=60)
    if out.returncode != 0:
        return {"error": out.stderr.strip()[-500:]}
    return json.loads(out.stdout)


def engine(path: Path) -> str:
    from sovander.engine import client
    from sovander.engine.process import Engine

    eng = Engine()
    try:
        chunks = list(client.convert(path.read_bytes(), client.TEXT, engine=eng))
        pdf = bytes(chunks[-1])
        return f"ok: {len(pdf)} bytes" if pdf.startswith(b"%PDF-") else "error: not a PDF"
    except Exception as exc:  # noqa: BLE001 - the probe reports, the test decides
        return f"error: {type(exc).__name__}: {exc}"
    finally:
        eng.stop()


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__,
                                     formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--libdir", default=str(Path(__file__).resolve().parents[2]),
                        help="folder holding the sovander package (default: this checkout)")
    parser.add_argument("--canary-port", type=int, help="127.0.0.1 port to connect to when a socket opens")
    parser.add_argument("--engine-file", type=Path, help="a Word file for the engine to convert")
    parser.add_argument("--child-only", action="store_true", help=argparse.SUPPRESS)
    args = parser.parse_args()

    result: dict = {"label": label(), "inet": inet(socket.AF_INET, args.canary_port)}
    if not args.child_only:
        sys.path.insert(0, args.libdir)
        from sovander import offline

        result.update(
            status=offline.check().value,
            module=offline.__file__,
            inet6=inet(socket.AF_INET6, None),
            unix=unix_round_trip(),
            child=child(args.canary_port),
            engine=engine(args.engine_file) if args.engine_file else None,
            python=sys.executable,
        )
    print(json.dumps(result))
    return 0


if __name__ == "__main__":
    sys.exit(main())
