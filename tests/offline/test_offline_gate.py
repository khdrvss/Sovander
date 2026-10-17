"""Offline gate (Requirements 9.1, 9.7, 9.8): the AppArmor profile keeps Sovander off the network.

Everywhere: the probe run unconfined must report UNPROTECTED and reach a canary listener on
127.0.0.1, which proves the probe and the canary can see a connection when there is one.
Where the profile is loaded (the CI offline job, after installing the .deb): the same probe inside
`aa-exec -p sovander` must report VERIFIED, its child must be denied too, AF_UNIX must still work,
the engine must convert a document over its pipe, and the canary must see nothing.

SOVANDER_REQUIRE_CONFINEMENT=1 turns "profile not loaded" from a skip into a failure, so the CI job
cannot pass by skipping. SOVANDER_PROBE_LIBDIR picks the sovander package the probe imports
(default: this checkout; CI: /usr/lib/sovander from the installed .deb).
"""

from __future__ import annotations

import json
import os
import re
import shutil
import socket
import subprocess
import sys
import threading
import time
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
PROBE = Path(__file__).with_name("probe.py")
LIBDIR = os.environ.get("SOVANDER_PROBE_LIBDIR") or str(ROOT)
INSTALLED_PROFILE = Path("/etc/apparmor.d/sovander")
INET_ALLOW = re.compile(r"^(audit\s+|allow\s+)*network\s*(inet6?\b|,|\s+(tcp|udp|raw))")
REQUIRE = os.environ.get("SOVANDER_REQUIRE_CONFINEMENT") == "1"
# The profile maps code only from /usr: a confined probe must use the system interpreter
SYSTEM_PYTHON = "/usr/bin/python3"


class Canary:
    """A listener on 127.0.0.1 that counts the connections it gets. It runs in this (unconfined) process."""

    def __init__(self) -> None:
        self.sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        self.sock.bind(("127.0.0.1", 0))
        self.sock.listen(8)
        self.sock.settimeout(0.2)
        self.port = self.sock.getsockname()[1]
        self.count = 0
        self._stop = threading.Event()
        self._thread = threading.Thread(target=self._serve, daemon=True)
        self._thread.start()

    def _serve(self) -> None:
        while not self._stop.is_set():
            try:
                conn, _ = self.sock.accept()
            except OSError:
                continue
            self.count += 1
            conn.close()

    def settle(self, expected: int = 0, timeout: float = 5.0) -> int:
        """The count, read once `expected` connections arrived (or `timeout` passed) plus half a second.

        A connect completes in the kernel before this thread accepts it, so the count can lag.
        """
        deadline = time.monotonic() + timeout
        while self.count < expected and time.monotonic() < deadline:
            time.sleep(0.05)
        time.sleep(0.5)
        return self.count

    def close(self) -> None:
        self._stop.set()
        self._thread.join(5)
        self.sock.close()


@pytest.fixture
def canary():
    listener = Canary()
    yield listener
    listener.close()


def current_label() -> str:
    for attr in ("/proc/self/attr/apparmor/current", "/proc/self/attr/current"):
        try:
            return Path(attr).read_text(encoding="utf-8", errors="replace").strip("\0\n ")
        except OSError:
            continue
    return ""


def profile_loaded() -> bool:
    if shutil.which("aa-exec") is None:
        return False
    entered = subprocess.run(["aa-exec", "-p", "sovander", "--", "/bin/true"], capture_output=True)
    return entered.returncode == 0


def need_profile() -> None:
    if profile_loaded():
        return
    reason = "the sovander AppArmor profile is not loaded (install the .deb on a machine with AppArmor)"
    if REQUIRE:
        pytest.fail(reason, pytrace=False)
    pytest.skip(reason)


def probe(*args: str, confined: bool) -> dict:
    python = SYSTEM_PYTHON if confined else sys.executable
    cmd = [python, "-I", "-B", str(PROBE), "--libdir", LIBDIR, *args]
    if confined:
        cmd = ["aa-exec", "-p", "sovander", "--", *cmd]
    out = subprocess.run(cmd, capture_output=True, text=True, timeout=300)
    assert out.returncode == 0, out.stderr
    return json.loads(out.stdout)


def test_unconfined_probe_sees_the_network(canary):
    if current_label().startswith("sovander"):
        pytest.skip("this test process is itself confined")
    result = probe("--canary-port", str(canary.port), confined=False)
    assert result["status"] == "unprotected", result
    assert result["inet"] == "connected", result
    assert result["child"]["inet"] == "connected", result
    assert result["unix"] == "ok", result
    assert canary.settle(expected=2) == 2  # the probe and its child: a connection is seen when there is one


def test_confined_probe_is_verified_and_children_inherit(canary):
    need_profile()
    result = probe("--canary-port", str(canary.port), confined=True)
    assert result["status"] == "verified", result
    assert result["inet"].startswith("denied:"), result
    assert result["inet6"].startswith("denied:"), result  # EAFNOSUPPORT on a kernel without IPv6
    assert result["child"].get("inet", "").startswith("denied:"), result
    assert result["unix"] == "ok", result  # local IPC (the engine's pipe, D-Bus, Wayland) still works
    assert "sovander" in (result["label"] or ""), result
    assert canary.settle() == 0


@pytest.mark.engine
def test_engine_converts_inside_the_profile(canary):
    """The engine reaches the app over AF_UNIX only, so denying inet must not break it (design risk 3)."""
    need_profile()
    document = ROOT / "tests" / "fixtures" / "report.docx"
    result = probe("--canary-port", str(canary.port), "--engine-file", str(document), confined=True)
    assert result["status"] == "verified", result
    assert (result["engine"] or "").startswith("ok:"), result
    assert canary.settle() == 0



def test_web_view_sandbox_starts_inside_the_profile():
    """WebKit puts its processes in a bwrap sandbox, started from inside the profile. Under the first
    installed profile bwrap could not build its root (/newroot), and every book or Markdown file
    crashed the app ("Failed to fully launch dbus-proxy"): found only by running the installed .deb."""
    need_profile()
    sandbox = ("/usr/bin/bwrap --unshare-user --unshare-net --ro-bind /usr /usr --symlink usr/lib64 /lib64"
               " --symlink usr/lib /lib --symlink usr/bin /bin --dir /etc --proc /proc --dev /dev"
               " /usr/bin/xdg-dbus-proxy --version")  # what WebKit starts; bwrap may run nothing else
    out = subprocess.run(["aa-exec", "-p", "sovander", "--", "/bin/sh", "-c", sandbox],
                         capture_output=True, text=True, timeout=60)
    assert out.returncode == 0, out.stderr

def profile_blocks(text: str) -> dict[str, list[str]]:
    """Rules per profile block, comments dropped (an abi4 variant has its "#abi4# " lines switched on)."""
    blocks = {}
    for block in re.split(r"^\s*profile\s+", text, flags=re.MULTILINE)[1:]:
        rules = [line.split("#", 1)[0].strip() for line in block.splitlines()[1:]]
        blocks[block.split()[0]] = [r for r in rules if r]
    return blocks


def test_installed_profile_still_denies_the_internet():
    """What postinst installed (either variant) keeps the promise in every block (9.8)."""
    if not INSTALLED_PROFILE.exists():
        if REQUIRE:
            pytest.fail(f"{INSTALLED_PROFILE} missing", pytrace=False)
        pytest.skip(f"{INSTALLED_PROFILE} not installed")
    blocks = profile_blocks(INSTALLED_PROFILE.read_text(encoding="utf-8"))
    assert set(blocks) == {"sovander", "bwrap"}, sorted(blocks)
    for name, rules in blocks.items():
        assert "deny network inet," in rules and "deny network inet6," in rules, name
        allowed = [r for r in rules if INET_ALLOW.match(r)]
        assert not allowed, (name, allowed)
