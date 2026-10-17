"""About window (Requirement 9.7, NFR Licensing): version, licence, credits and the offline status.

Gtk.AboutDialog, not Adw.AboutWindow, which needs libadwaita 1.2 (Ubuntu 22.04 has 1.1). Its
License page holds the full texts: Sovander's file-by-file notices with the AGPL-3.0 in full
(debian/copyright) and the licences of the bundled web assets (data/assets/LICENSES.md). The
comment under the name says whether the system blocks the network now, and which AppArmor profile
the app runs in. Any link in the dialog goes through the same confirmation as a document's (9.3).
"""

from __future__ import annotations

import platform
from dataclasses import dataclass
from pathlib import Path

import gi

gi.require_version("Adw", "1")
gi.require_version("Gtk", "4.0")
from gi.repository import Adw, Gdk, Gtk  # noqa: E402

from sovander import APP_ID, __version__, links, offline  # noqa: E402
from sovander.i18n import N_, _  # noqa: E402
from sovander.offline import OfflineStatus  # noqa: E402

try:
    import pymupdf as fitz
except ImportError:  # PyMuPDF before 1.24 (Ubuntu 22.04 and 24.04) has only the old name
    import fitz

ROOT = Path(__file__).resolve().parent.parent
# Installed (packaging/build-deb.sh) first, then a source checkout
HOMEPAGE = "https://github.com/khdrvss/Sovander"
COPYRIGHT_FILES = (Path("/usr/share/doc/sovander/copyright"), ROOT / "packaging" / "debian" / "copyright")
ASSET_LICENCES = ROOT / "data" / "assets" / "LICENSES.md"

# Licences as each project's own or Debian's copyright file states them (checked 2026-10-17)
CREDITS = (
    (N_("Based on"), ("Gander, © 2026 Arjun Maniyani (MIT)",)),
    (N_("Document engines"), (
        "LibreOffice (MPL-2.0)",
        "MuPDF and PyMuPDF, Artifex (AGPL-3.0)",
        "WebKitGTK (LGPL-2.1 and BSD-2-Clause)",
    )),
    (N_("Bundled libraries"), (
        "marked, Christopher Jeffrey (MIT)",
        "DOMPurify, Cure53 (Apache-2.0)",
        "JSZip, Stuart Knightley, with pako (MIT)",
    )),
    (N_("Fonts"), ("Carlito (OFL-1.1)", "Caladea (OFL-1.1)")),
)


@dataclass(frozen=True)
class AboutInfo:
    comments: str
    licence: str
    system: str
    credits: tuple[tuple[str, tuple[str, ...]], ...]


def offline_lines(status: OfflineStatus, label: str | None) -> str:
    """The offline sentence and the AppArmor profile, one per line."""
    profile = _("AppArmor profile: {label}").format(label=label) if label else _("AppArmor: not available")
    return f"{offline.message(status)}\n{profile}"


def licence_text(copyright_files: tuple[Path, ...] = COPYRIGHT_FILES, assets: Path = ASSET_LICENCES) -> str:
    """Every licence text Sovander ships, with a note naming any file that is missing."""
    parts = [_("Sovander is free software under the GNU Affero General Public License, version 3 or "
               "later. The full texts of its licence and of the licences of the parts it bundles follow.")]
    copyright_file = next((p for p in copyright_files if p.is_file()), copyright_files[-1])
    missing = []
    for path in (copyright_file, assets):
        try:
            parts.append(path.read_text(encoding="utf-8"))
        except OSError:
            missing.append(str(path))
    if missing:
        parts.append(_("These licence files are missing from this installation: {files}").format(
            files=", ".join(missing)))
    return "\n\n".join(parts)


def about_info(status: OfflineStatus | None = None) -> AboutInfo:
    """What the About window shows; checks the offline status now unless given."""
    status = offline.check() if status is None else status
    label = offline.confinement()
    comments = _("Reads office documents, PDFs, books and images. Never edits them, never goes online.")
    versions = (
        f"Sovander {__version__}",
        f"GTK {Gtk.get_major_version()}.{Gtk.get_minor_version()}.{Gtk.get_micro_version()}",
        f"libadwaita {Adw.get_major_version()}.{Adw.get_minor_version()}.{Adw.get_micro_version()}",
        f"PyMuPDF {fitz.VersionBind}, MuPDF {fitz.VersionFitz}",
        f"Python {platform.python_version()}",
    )
    return AboutInfo(
        comments=f"{comments}\n\n{offline_lines(status, label)}",
        licence=licence_text(),
        system="\n".join((offline_lines(status, label), "", *versions)),
        credits=tuple((_(title), people) for title, people in CREDITS),
    )


def show_about(parent: Gtk.Window | None, status: OfflineStatus | None = None) -> Gtk.AboutDialog:
    """Show the About window over ``parent`` and return it."""
    info = about_info(status)
    dialog = Gtk.AboutDialog(
        transient_for=parent,
        modal=True,
        program_name="Sovander",
        version=__version__,
        comments=info.comments,
        copyright="© 2026 Saidxon Xaydarov",
        website=HOMEPAGE,  # through _on_link: asked first, like any link
        license=info.licence,
        wrap_license=True,
        system_information=info.system,
        authors=["Saidxon Xaydarov"],
        logo_icon_name=_logo(),
    )
    for title, people in info.credits:
        dialog.add_credit_section(title, list(people))
    dialog.connect("activate-link", _on_link)
    dialog.present()
    return dialog


def _logo() -> str:
    display = Gdk.Display.get_default()
    if display is not None and Gtk.IconTheme.get_for_display(display).has_icon(APP_ID):
        return APP_ID
    return "x-office-document"


def _on_link(dialog: Gtk.AboutDialog, uri: str) -> bool:
    """Ask first, as for a link in a document (9.3); never open from here directly."""
    links.confirm_and_open(dialog, uri)
    return True
