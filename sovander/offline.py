"""Offline self-check (Requirements 9.1, 9.6, 9.7): is the network denied by the system right now?

The AppArmor profile says ``deny network inet,`` and ``deny network inet6,``. AppArmor applies
network rules when a socket is created, so a confined process cannot even create an internet
socket: ``socket()`` fails with EACCES (EPERM when a seccomp filter denies it instead).

The probe therefore only creates sockets and closes them at once. It never binds, connects or
sends, so it cannot put a packet on any interface, loopback included: creating a socket is
bookkeeping inside the kernel. A probe that could create a socket proves the app is not confined,
and that is reported as is. Going further (a connect to see whether it fails) would mean traffic.

Stream and datagram sockets are both probed, since a profile that denied only one would still let
DNS or other UDP out. A family the kernel lacks (EAFNOSUPPORT) is as unusable as a denied one.
"""

from __future__ import annotations

import errno
import logging
import socket
from enum import Enum
from pathlib import Path

from sovander.i18n import N_, _

log = logging.getLogger(__name__)

PROBES = (
    (socket.AF_INET, socket.SOCK_STREAM),
    (socket.AF_INET, socket.SOCK_DGRAM),
    (socket.AF_INET6, socket.SOCK_STREAM),
    (socket.AF_INET6, socket.SOCK_DGRAM),
)
DENIED = frozenset({errno.EACCES, errno.EPERM})
ABSENT = frozenset({errno.EAFNOSUPPORT})

_new_socket = socket.socket


class OfflineStatus(Enum):
    VERIFIED = "verified"
    UNPROTECTED = "unprotected"
    UNKNOWN = "unknown"


_MESSAGES = {
    OfflineStatus.VERIFIED: N_("Offline: verified by the system"),
    OfflineStatus.UNPROTECTED: N_("Offline protection is off: the network is not blocked"),
    OfflineStatus.UNKNOWN: N_("Offline protection could not be checked"),
}


def check() -> OfflineStatus:
    """Probe every internet socket kind; VERIFIED only when the system refuses all of them."""
    outcomes = {(family.name, kind.name): _probe(family, kind) for family, kind in PROBES}
    if "open" in outcomes.values():
        status = OfflineStatus.UNPROTECTED
    elif set(outcomes.values()) <= {"denied", "absent"} and "denied" in outcomes.values():
        status = OfflineStatus.VERIFIED
    else:
        status = OfflineStatus.UNKNOWN
    # Kept in the local log only; nothing here is ever sent anywhere (9.6)
    log.log(
        logging.INFO if status is OfflineStatus.VERIFIED else logging.WARNING,
        "offline self-check: %s %s, confinement: %s",
        status.value,
        outcomes,
        confinement(),
    )
    return status


def message(status: OfflineStatus) -> str:
    """The sentence for About and the warning banner, in the current language."""
    return _(_MESSAGES[status])


def confinement() -> str | None:
    """The AppArmor label of this process, such as "sovander (enforce)" or "unconfined"."""
    for attr in ("/proc/self/attr/apparmor/current", "/proc/self/attr/current"):
        try:
            label = Path(attr).read_text(encoding="utf-8", errors="replace").strip("\0\n ")
        except OSError:
            continue
        if label:
            return label
    return None


def _probe(family: socket.AddressFamily, kind: socket.SocketKind) -> str:
    try:
        sock = _new_socket(family, kind)
    except OSError as e:
        if e.errno in DENIED:
            return "denied"
        if e.errno in ABSENT:
            return "absent"
        log.warning("offline probe %s/%s failed unexpectedly: %s", family.name, kind.name, e)
        return "error"
    sock.close()
    return "open"
