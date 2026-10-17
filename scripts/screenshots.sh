#!/usr/bin/env bash
# Screenshots for the README and the metainfo, from the real app and the project's own documents
# (docs/showcase, made by scripts/make-showcase.py): no one else's files.
#   scripts/screenshots.sh [en|ru]      → docs/screenshots/<lang>/*.png
# The installed Sovander when there is one (confined: the pictures show the offline lock really on),
# else the checkout. Needs xvfb-run, xdotool and ImageMagick's import; the engine is the installed LibreOffice.
set -euo pipefail
REPO="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
lang=${1:-en}
case $lang in
  en) locale=en_US.UTF-8 ;;
  ru) locale=ru_RU.UTF-8 ;;
  *) echo "usage: $0 [en|ru]" >&2; exit 2 ;;
esac
out="$REPO/docs/screenshots/$lang"
mkdir -p "$out" "$REPO/build"
if [ -x /usr/bin/sovander ]; then
  run=(/usr/bin/sovander)
  base="$HOME/.cache/io.github.khdrvss.Sovander"  # inside what the AppArmor profile lets the app write
else
  run=(python3 -m sovander)
  base="$REPO/build"
fi
mkdir -p "$base"
SOVANDER_HOME="$(mktemp -d "$base/screenshots-XXXXXX")"  # a fresh home: only these files in Recent
export SOVANDER_HOME ADW_DEBUG_COLOR_SCHEME=prefer-light LANG=$locale LANGUAGE=
S="$REPO/docs/showcase"

# shot NAME SECONDS [FILE]: launch, size the window, wait, capture the window alone, quit through SIGTERM
shot() {
  local name=$1 wait=$2 file=${3:-}
  xvfb-run -a -s "-screen 0 1400x900x24" bash -c '
    cd "$0" || exit 1
    "${@:4}" ${3:+"$3"} >"$1.log" 2>&1 &
    app=$!
    win=$(xdotool search --sync --onlyvisible --pid "$app" | head -1)
    xdotool windowsize "$win" 1200 780 windowmove "$win" 0 0
    sleep "$2"
    import -window "$win" "$1"
    kill -TERM "$app"; wait "$app" || true' "$REPO" "$out/$name.png" "$wait" "$file" "${run[@]}"
  rm -f "$out/$name.png.log"
  echo "$out/$name.png"
}

shot document 6 "$S/report.docx"
shot spreadsheet 6 "$S/budget.xlsx"
shot slides 7 "$S/talk.pptx"
shot book 5 "$S/almanac.epub"
shot home 4  # last: Recent now holds the files above, with their thumbnails
echo "made with: ${run[*]}"
