#!/usr/bin/env bash
# Downloads the spike's reference corpus (Apache POI test data, Gander samples, the 756-page
# PDF 32000-2008) exactly as tests/fidelity/corpus.lock records it: URLs pinned to a commit and a
# sha256 per file, so every machine grades the same bytes. Files already there with the right hash
# are kept; anything else is downloaded again. Tests never download: without the corpus they skip.
#
#   scripts/fetch-corpus.sh [DEST]     DEST defaults to spike/corpus (where the tests look)
#   SOVANDER_CORPUS_LOCK=FILE          use another lock file (to test this script)
#
# The files are untrusted input (many are fuzzer crash cases): they are written, never run.
set -euo pipefail

repo=$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)
lock=${SOVANDER_CORPUS_LOCK:-$repo/tests/fidelity/corpus.lock}
dest=${1:-$repo/spike/corpus}

command -v curl >/dev/null 2>&1 || {
    echo "fetch-corpus: curl is needed" >&2
    exit 1
}
mkdir -p "$dest"
tmp=$(mktemp -d "${TMPDIR:-/tmp}/sovander-corpus.XXXXXX")
trap 'rm -rf -- "$tmp"' EXIT

sha256_of() { sha256sum <"$1" | cut -d' ' -f1; }

total=0 fetched=0 kept=0 line=0
while IFS=$'\t' read -r sha size path url; do
    line=$((line + 1))
    [[ -z $sha || $sha == \#* ]] && continue
    # The lock is data too: nothing in it may write outside DEST or leave HTTPS
    if [[ ! $sha =~ ^[0-9a-f]{64}$ || ! $size =~ ^[0-9]+$ || -z $path || $path == /* ||
        $path == *..* || ! $url =~ ^https:// ]]; then
        echo "fetch-corpus: $lock:$line: malformed entry" >&2
        exit 1
    fi
    total=$((total + 1))
    target=$dest/$path
    if [[ -f $target && $(sha256_of "$target") == "$sha" ]]; then
        kept=$((kept + 1))
        continue
    fi
    mkdir -p "$(dirname "$target")"
    curl --fail --silent --show-error --location --proto '=https' --proto-redir '=https' \
        --retry 3 --retry-delay 2 --connect-timeout 20 --max-time 600 --max-filesize 104857600 \
        --output "$tmp/download" "$url"
    got=$(sha256_of "$tmp/download")
    if [[ $got != "$sha" ]]; then
        echo "fetch-corpus: $path: sha256 $got, the lock says $sha ($url)" >&2
        exit 1
    fi
    mv -f "$tmp/download" "$target"
    fetched=$((fetched + 1))
done <"$lock"

if ((total == 0)); then
    echo "fetch-corpus: no entries in $lock" >&2
    exit 1
fi
echo "fetch-corpus: $total files verified in $dest ($fetched downloaded, $kept already present)"
