#!/usr/bin/env bash
# Development only: run a command with the LibreOffice to test against as the engine.
# The packaged app uses Ubuntu's python3-uno and /usr/bin/soffice and needs none of this.
#   scripts/dev-env.sh python3 -m pytest -m engine                   # Ubuntu's LibreOffice when installed,
#                                                                    # else the snap (default)
#   SOVANDER_DEV_ENGINE=snap scripts/dev-env.sh python3 -m pytest ... # the snap LibreOffice
#   scripts/dev-env.sh --fetch-deb                                   # unpack the .deb engine, no root
#   SOVANDER_DEV_ENGINE=deb scripts/dev-env.sh python3 -m pytest ... # run against it
# The .deb engine is the product's own (Ubuntu's libreoffice-*-nogui and python3-uno), for numbers the
# snap would distort (cold start). --fetch-deb: apt-get download (hash-checked against the signed
# index) into build/lo-deb/debs, dpkg -x into build/lo-deb/root, the registry copied to etc/ as the
# packages' ucf step would, and the hard-coded /usr/lib/libreoffice and /etc/libreoffice paths pointed
# into the unpacked tree. No maintainer script runs; only soffice and the uno modules are used.
set -euo pipefail
REPO="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
DEB_ROOT="${DEB_ROOT:-$REPO/build/lo-deb/root}"
DEB_PACKAGES="libreoffice-core-nogui libreoffice-writer-nogui libreoffice-calc-nogui libreoffice-impress-nogui
 libreoffice-draw-nogui libreoffice-common python3-uno uno-libs-private ure libreoffice-style-colibre"

fetch_deb() {
  local debs="$DEB_ROOT/../debs" program="$DEB_ROOT/usr/lib/libreoffice/program" link
  if [ -x "$program/soffice" ]; then
    echo "already unpacked in $DEB_ROOT"; return
  fi
  mkdir -p "$debs" "$DEB_ROOT"
  # shellcheck disable=SC2086 # the package list is word-split on purpose
  mapfile -t missing < <(apt-get -s install $DEB_PACKAGES | awk '/^Inst /{print $2}')
  (cd "$debs" && apt-get download "${missing[@]}")
  for deb in "$debs"/*.deb; do dpkg -x "$deb" "$DEB_ROOT"; done
  cp -a "$DEB_ROOT/usr/lib/libreoffice/share/.registry/." "$DEB_ROOT/etc/libreoffice/registry/"
  find "$DEB_ROOT" -type l -lname '/*' | while read -r link; do
    ln -sfn "$DEB_ROOT$(readlink "$link")" "$link"
  done
  sed -i "s|file:///usr/lib/libreoffice|file://$DEB_ROOT/usr/lib/libreoffice|;
          s|file:///etc/libreoffice|file://$DEB_ROOT/etc/libreoffice|g" "$program/fundamentalrc" "$program/sofficerc"
  sed -i "s|'/usr/lib/libreoffice|'$DEB_ROOT/usr/lib/libreoffice|; s|:/usr/lib/libreoffice|:$DEB_ROOT/usr/lib/libreoffice|" \
    "$DEB_ROOT/usr/lib/python3/dist-packages/uno.py"
  echo "unpacked ${#missing[@]} packages into $DEB_ROOT"
}

if [ "${1:-}" = "--fetch-deb" ]; then
  fetch_deb
  exit 0
fi
MODE="${SOVANDER_DEV_ENGINE:-}"
if [ -z "$MODE" ]; then
  if [ -x /usr/lib/libreoffice/program/soffice ]; then MODE=system; else MODE=snap; fi
fi
if [ "$MODE" = "system" ]; then
  exec "$@"  # what the packaged app runs on: nothing to point anywhere
fi
if [ "$MODE" = "deb" ]; then
  LO_PROGRAM="$DEB_ROOT/usr/lib/libreoffice/program"
  [ -x "$LO_PROGRAM/soffice" ] || { echo "no .deb engine in $DEB_ROOT: run $0 --fetch-deb" >&2; exit 1; }
  MULTIARCH="$(python3 -c 'import sysconfig; print(sysconfig.get_config_var("MULTIARCH"))')"
  export SOVANDER_SOFFICE="$LO_PROGRAM/soffice"
  export LD_LIBRARY_PATH="$LO_PROGRAM:$DEB_ROOT/usr/lib/$MULTIARCH${LD_LIBRARY_PATH:+:$LD_LIBRARY_PATH}"
  # only uno.py and unohelper.py live here; uno.py appends program/ (pyuno) after the standard library
  export PYTHONPATH="$DEB_ROOT/usr/lib/python3/dist-packages${PYTHONPATH:+:$PYTHONPATH}"
else
  LO_PROGRAM="${LO_PROGRAM:-/snap/libreoffice/current/lib/libreoffice/program}"
  export SOVANDER_SOFFICE="${SOVANDER_SOFFICE:-/snap/bin/libreoffice}"
  export LD_LIBRARY_PATH="$LO_PROGRAM${LD_LIBRARY_PATH:+:$LD_LIBRARY_PATH}"
  export PYTHONPATH="$LO_PROGRAM${PYTHONPATH:+:$PYTHONPATH}"
fi
export URE_BOOTSTRAP="vnd.sun.star.pathname:$LO_PROGRAM/fundamentalrc"
exec "$@"
