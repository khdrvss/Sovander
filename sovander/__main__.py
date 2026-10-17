"""``python3 -m sovander [FILE…]``: start the viewer (the launcher packaging/sovander.sh runs this)."""

import sys


def main(argv: list[str] | None = None) -> int:
    from gi.repository import GLib

    # First of all: WebKit and GLib name per-program folders after it, and they read it once
    GLib.set_prgname("sovander")
    from sovander.app import run

    return run(sys.argv if argv is None else argv)


if __name__ == "__main__":
    sys.exit(main())
