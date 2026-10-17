#!/bin/bash
# Builds dist/sovander-offline-<version>-<codename>.tar: the sovander .deb plus every .deb it needs on
# a fresh Ubuntu of the same release, so a computer with no internet installs it with one command
# (Requirement 13.3).
#
# Run it, without root, on an up-to-date Ubuntu of the target release (22.04 bundles for 22.04,
# 24.04 for 24.04) that has internet and current package lists (sudo apt update).
#
#   packaging/make-offline-bundle.sh [--dry-run] [--out DIR] [path/to/sovander.deb]
#
# --dry-run lists what would be downloaded and stops. --out puts the archive in DIR instead of dist/.
# Without a .deb, packaging/build-deb.sh makes one.
set -euo pipefail

usage() { sed -n '2,13s/^# \{0,1\}//p' "${BASH_SOURCE[0]}"; }

repo=$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)
dry_run=false
deb=""
out=$repo/dist
while (($#)); do
    case $1 in
    --dry-run) dry_run=true ;;
    --out)
        out=${2:?make-offline-bundle: --out needs a folder}
        shift
        ;;
    -h | --help) usage && exit 0 ;;
    -*) echo "make-offline-bundle: unknown option $1" >&2 && exit 2 ;;
    *) deb=$1 ;;
    esac
    shift
done

[[ -n $deb ]] || deb=$("$repo/packaging/build-deb.sh")
deb=$(realpath "$deb")
version=$(dpkg-deb -f "$deb" Version)

# shellcheck source=/dev/null
. /etc/os-release
codename=${VERSION_CODENAME:?make-offline-bundle: no VERSION_CODENAME in /etc/os-release}
if [[ $(dpkg --print-architecture) != amd64 ]]; then
    echo "make-offline-bundle: Sovander v1 targets x86-64; run this on an amd64 Ubuntu" >&2
    exit 1
fi

work=$(mktemp -d "${TMPDIR:-/tmp}/sovander-bundle.XXXXXX")
trap 'rm -rf -- "$work"' EXIT

# Resolve against an empty package database: apt itself picks every package, alternative and
# provider a fresh system would need. Recommends stay out; INSTALL.txt turns them off too.
: >"$work/status"
apt-get -s -q -o Dir::State::status="$work/status" -o Debug::NoLocking=1 \
    -o APT::Install-Recommends=false install "$deb" >"$work/simulation"
sed -nE 's/^Inst ([^ ]+) \(([^ ]+) .*\[([^]]+)\]\).*/\1:\3=\2/p' "$work/simulation" |
    grep -v '^sovander:' >"$work/packages" || true
count=$(wc -l <"$work/packages")
if ((count == 0)); then
    echo "make-offline-bundle: apt resolved no dependencies; are the package lists there?" >&2
    exit 1
fi

if $dry_run; then
    cat "$work/packages"
    # Sizes of exactly the resolved versions; shown so the download is not a surprise
    xargs apt-cache show --no-all-versions <"$work/packages" 2>/dev/null |
        awk -v n="$count" '/^Size:/ { s += $2 } END { printf "%d packages, %.0f MB to download\n", n, s / 1048576 }' >&2
    exit 0
fi

name=sovander-offline-$version-$codename
bundle=$work/$name
mkdir -p "$bundle"
# apt-get download checks every file against the signed package index
(cd "$bundle" && xargs apt-get download -q <"$work/packages")
cp "$deb" "$bundle/"
got=$(find "$bundle" -name '*.deb' | wc -l)
if ((got != count + 1)); then
    echo "make-offline-bundle: expected $((count + 1)) packages, got $got" >&2
    exit 1
fi

cat >"$bundle/INSTALL.txt" <<EOF
Sovander $version for Ubuntu $VERSION_ID ($codename), x86-64: installation without internet

1. Copy this folder to the computer.
2. Open a terminal in this folder and run:

   sudo apt install --no-install-recommends ./*.deb

No internet is needed: every package Sovander uses is in this folder. --no-install-recommends stops
apt from trying to download optional extras. To remove Sovander: sudo apt remove sovander
This folder is for Ubuntu $VERSION_ID only.

---

Sovander $version для Ubuntu $VERSION_ID ($codename), x86-64: установка без интернета

1. Скопируйте эту папку на компьютер.
2. Откройте терминал в этой папке и выполните:

   sudo apt install --no-install-recommends ./*.deb

Интернет не нужен: все пакеты, которые использует Sovander, лежат в этой папке.
--no-install-recommends не даёт apt пытаться скачать необязательные дополнения.
Удалить Sovander: sudo apt remove sovander
Эта папка подходит только для Ubuntu $VERSION_ID.
EOF

mkdir -p "$out"
tarball=$out/$name.tar
tar --sort=name --owner=0 --group=0 --numeric-owner -C "$work" -cf "$tarball" "$name"
echo "$tarball"
