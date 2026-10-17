#!/bin/bash
# Builds dist/sovander_<version>_all.deb from this checkout, without root (dpkg-deb --root-owner-group).
#
# Layout (Requirement 13, design "File Structure Plan"):
#   /usr/bin/sovander                     launcher (packaging/sovander.sh)
#   /usr/lib/sovander/sovander/           the Python package
#   /usr/lib/sovander/data -> ../../share/sovander
#                                         the app finds data/ beside its package, as in a checkout
#   /usr/share/sovander/                  data/ (engine profile, web assets) and both AppArmor variants
#   /usr/share/locale/<lang>/LC_MESSAGES/sovander.mo
#   /usr/share/applications/io.github.khdrvss.Sovander.desktop, metainfo, hicolor icon, doc/sovander/
# /etc/apparmor.d/sovander is not in the archive: postinst copies the variant the system's AppArmor
# understands (packaging/apparmor/sovander explains why there are two).
set -euo pipefail

repo=$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)
pkg=$repo/packaging
out=${1:-$repo/dist}
readonly MAX_INSTALLED_KB=20480 # NFR Size: the Viewer's own installed files 20 MB or less

# The version comes from the changelog and must match the app's own
version=$(sed -n '1s/^sovander (\([^)]*\)).*/\1/p' "$pkg/debian/changelog")
app_version=$(sed -n 's/^__version__ = "\(.*\)"/\1/p' "$repo/sovander/__init__.py")
if [[ -z $version || $version != "$app_version" ]]; then
    echo "build-deb: changelog version '$version' differs from sovander.__version__ '$app_version'" >&2
    exit 1
fi
# File times from the changelog date, so the same checkout builds the same archive
SOURCE_DATE_EPOCH=$(date -d "$(sed -n 's/^ -- .*>  //p' "$pkg/debian/changelog" | head -n1)" +%s)
export SOURCE_DATE_EPOCH

stage=$(mktemp -d "${TMPDIR:-/tmp}/sovander-deb.XXXXXX")
trap 'rm -rf -- "$stage"' EXIT
root=$stage/sovander_${version}_all

# Python package, without bytecode (postinst compiles it on the target)
mkdir -p "$root/usr/lib/sovander"
tar -C "$repo" --exclude=__pycache__ --exclude='*.py[co]' -cf - sovander | tar -C "$root/usr/lib/sovander" -xf -
ln -s ../../share/sovander "$root/usr/lib/sovander/data"

# data/ minus what has a standard place of its own
mkdir -p "$root/usr/share/sovander"
tar -C "$repo/data" --exclude='*.desktop' --exclude='*.metainfo.xml' --exclude=icons -cf - . |
    tar -C "$root/usr/share/sovander" -xf -

# AppArmor: the 3.0 profile as written, and the 4.0 variant with its "#abi4# " rules switched on
mkdir -p "$root/usr/share/sovander/apparmor"
cp "$pkg/apparmor/sovander" "$root/usr/share/sovander/apparmor/sovander"
sed -e 's|^abi <abi/3\.0>,|abi <abi/4.0>,|' -e 's|^\([[:space:]]*\)#abi4# |\1|' \
    "$pkg/apparmor/sovander" >"$root/usr/share/sovander/apparmor/sovander.abi4"
grep -q '^abi <abi/4.0>,' "$root/usr/share/sovander/apparmor/sovander.abi4"

install -D -m 0755 "$pkg/sovander.sh" "$root/usr/bin/sovander"
install -D -m 0644 "$repo/data/io.github.khdrvss.Sovander.desktop" "$root/usr/share/applications/io.github.khdrvss.Sovander.desktop"
install -D -m 0644 "$repo/data/io.github.khdrvss.Sovander.metainfo.xml" \
    "$root/usr/share/metainfo/io.github.khdrvss.Sovander.metainfo.xml"
(cd "$repo/data" && find icons -type f -print0) | while IFS= read -r -d '' icon; do
    install -D -m 0644 "$repo/data/$icon" "$root/usr/share/$icon"
done

# Translations: msgfmt when installed, otherwise the app's own compiler (no build tools needed)
while read -r lang; do
    [[ -z $lang || $lang == \#* ]] && continue
    mo=$root/usr/share/locale/$lang/LC_MESSAGES/sovander.mo
    mkdir -p "$(dirname "$mo")"
    if command -v msgfmt >/dev/null 2>&1; then
        msgfmt --check -o "$mo" "$repo/po/$lang.po"
    else
        python3 -I -c 'import sys
sys.path.insert(0, sys.argv[1])
from pathlib import Path
from sovander.i18n import compile_po
Path(sys.argv[3]).write_bytes(compile_po(Path(sys.argv[2]).read_text(encoding="utf-8")))' \
            "$repo" "$repo/po/$lang.po" "$mo"
    fi
done <"$repo/po/LINGUAS"

install -D -m 0644 "$pkg/debian/copyright" "$root/usr/share/doc/sovander/copyright"
gzip -9n <"$pkg/debian/changelog" >"$root/usr/share/doc/sovander/changelog.gz"

# Ownership is set by --root-owner-group; modes must not depend on the builder's umask
find "$root" -type d -exec chmod 0755 {} +
find "$root" -type f -exec chmod 0644 {} +
chmod 0755 "$root/usr/bin/sovander"

installed_kb=$(du -sk --apparent-size "$root" | cut -f1)
if ((installed_kb > MAX_INSTALLED_KB)); then
    echo "build-deb: installed size ${installed_kb} KB is over the ${MAX_INSTALLED_KB} KB budget" >&2
    exit 1
fi

mkdir -p "$root/DEBIAN"
sed -e "s/@VERSION@/$version/" -e "s/@INSTALLED_SIZE@/$installed_kb/" \
    "$pkg/debian/control" >"$root/DEBIAN/control"
for script in postinst prerm postrm; do
    install -m 0755 "$pkg/debian/$script" "$root/DEBIAN/$script"
done
(cd "$root" && find . -path ./DEBIAN -prune -o -type f -printf '%P\0' | sort -z | xargs -0 md5sum) \
    >"$root/DEBIAN/md5sums"
chmod 0644 "$root/DEBIAN/md5sums"
find "$root" -exec touch -h -d "@$SOURCE_DATE_EPOCH" {} +

mkdir -p "$out"
deb=$out/sovander_${version}_all.deb
dpkg-deb --root-owner-group -Zxz --build "$root" "$deb" >/dev/null
echo "$deb"
