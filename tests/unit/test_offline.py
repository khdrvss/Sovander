import errno
import socket

import pytest

from sovander import i18n, offline
from sovander.offline import OfflineStatus


class FakeSocket:
    """A socket that may only be closed: any network call fails the test."""

    def __init__(self, family, kind):
        self.family, self.kind = family, kind
        self.closed = False

    def close(self):
        self.closed = True

    def __getattr__(self, name):
        raise AssertionError(f"the probe must not call socket.{name}")


def fake_kernel(monkeypatch, refuse):
    """Patch socket creation: refuse(family, kind) returns an errno to fail with, or None to allow."""
    created = []

    def new_socket(family, kind):
        code = refuse(family, kind)
        if code is not None:
            raise OSError(code, "refused")
        sock = FakeSocket(family, kind)
        created.append(sock)
        return sock

    monkeypatch.setattr(offline, "_new_socket", new_socket)
    return created


def test_apparmor_denial_is_verified(monkeypatch):
    fake_kernel(monkeypatch, lambda f, k: errno.EACCES)
    assert offline.check() is OfflineStatus.VERIFIED


def test_seccomp_style_eperm_is_verified(monkeypatch):
    fake_kernel(monkeypatch, lambda f, k: errno.EPERM)
    assert offline.check() is OfflineStatus.VERIFIED


def test_kernel_without_ipv6_still_verified(monkeypatch):
    fake_kernel(monkeypatch, lambda f, k: errno.EACCES if f == socket.AF_INET else errno.EAFNOSUPPORT)
    assert offline.check() is OfflineStatus.VERIFIED


def test_unconfined_is_unprotected_and_sends_nothing(monkeypatch):
    created = fake_kernel(monkeypatch, lambda f, k: None)
    assert offline.check() is OfflineStatus.UNPROTECTED
    assert len(created) == 4
    assert all(s.closed for s in created)  # closed straight away; any connect/send would have raised


def test_one_open_kind_is_enough_to_be_unprotected(monkeypatch):
    # A profile that denies TCP but forgets UDP still lets DNS out
    fake_kernel(monkeypatch, lambda f, k: None if k == socket.SOCK_DGRAM else errno.EACCES)
    assert offline.check() is OfflineStatus.UNPROTECTED


def test_ipv6_left_open_is_unprotected(monkeypatch):
    fake_kernel(monkeypatch, lambda f, k: None if f == socket.AF_INET6 else errno.EACCES)
    assert offline.check() is OfflineStatus.UNPROTECTED


@pytest.mark.parametrize("code", [errno.EMFILE, errno.ENOBUFS, errno.EINVAL])
def test_unexpected_errors_are_unknown(monkeypatch, code):
    fake_kernel(monkeypatch, lambda f, k: code)
    assert offline.check() is OfflineStatus.UNKNOWN


def test_nothing_denied_but_all_families_absent_is_unknown(monkeypatch):
    fake_kernel(monkeypatch, lambda f, k: errno.EAFNOSUPPORT)
    assert offline.check() is OfflineStatus.UNKNOWN


def test_probes_cover_both_families_and_both_kinds(monkeypatch):
    seen = []

    def record(family, kind):
        seen.append((family, kind))
        return errno.EACCES

    fake_kernel(monkeypatch, record)
    offline.check()
    assert set(seen) == {
        (socket.AF_INET, socket.SOCK_STREAM),
        (socket.AF_INET, socket.SOCK_DGRAM),
        (socket.AF_INET6, socket.SOCK_STREAM),
        (socket.AF_INET6, socket.SOCK_DGRAM),
    }


def test_real_check_returns_a_status():
    # On a development machine this is normally UNPROTECTED; it creates sockets and sends nothing
    assert isinstance(offline.check(), OfflineStatus)


def test_messages_follow_the_language():
    i18n.set_language("en")
    english = {s: offline.message(s) for s in OfflineStatus}
    i18n.set_language("ru")
    russian = {s: offline.message(s) for s in OfflineStatus}
    i18n.set_language("en")
    assert english[OfflineStatus.VERIFIED] == "Offline: verified by the system"
    assert all(english[s] != russian[s] for s in OfflineStatus)


def test_confinement_reads_a_label_or_nothing():
    label = offline.confinement()
    assert label is None or isinstance(label, str)
