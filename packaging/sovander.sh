#!/bin/bash
# Sovander launcher, installed as /usr/bin/sovander. AGPL-3.0-or-later.
#
# Starts the app inside the AppArmor profile "sovander", which denies all internet access to the app
# and every process it starts (Requirements 9.1, 9.7). If AppArmor is off, aa-exec is missing or the
# profile is not loaded, the app still starts, unconfined, with SOVANDER_CONFINEMENT=none so it can
# show that offline protection is off. The app's own self-check (sovander/offline.py) proves the real
# state either way; the variable is only a hint.
#
#   SOVANDER_CONFINEMENT  set for the app: "apparmor" or "none"
#   SOVANDER_LIBDIR       where the sovander package lives (default /usr/lib/sovander; for tests)
set -euo pipefail

readonly PROFILE=sovander
libdir=${SOVANDER_LIBDIR:-/usr/lib/sovander}

# The equivalent of "python3 -m sovander" in isolated mode (-I): -m would put the current folder
# first on the import path, so a folder named "sovander" wherever the user starts the app (an
# unpacked download, say) would run instead of it. -I also keeps user site-packages and PYTHON*
# variables out, so only the Ubuntu packages the app depends on are imported.
app=(/usr/bin/python3 -I -c '
import runpy, sys
sys.path.insert(0, sys.argv.pop(1))
runpy.run_module("sovander", run_name="__main__", alter_sys=True)
' "$libdir")

current_label() {
    local attr
    for attr in /proc/self/attr/apparmor/current /proc/self/attr/current; do
        if [[ -r $attr ]]; then
            tr -d '\0' <"$attr" 2>/dev/null && return 0
        fi
    done
    return 1
}

# Already inside the profile (started from Sovander itself): nothing to enter
if [[ $(current_label || true) == "$PROFILE"* ]]; then
    export SOVANDER_CONFINEMENT=apparmor
    exec "${app[@]}" "$@"
fi

# Try the profile with a no-op first: once exec replaces this shell there is no way back to the
# unconfined start, and aa-exec fails when the profile is not loaded or AppArmor is off
if command -v aa-exec >/dev/null 2>&1 && aa-exec -p "$PROFILE" -- /bin/true >/dev/null 2>&1; then
    export SOVANDER_CONFINEMENT=apparmor
    exec aa-exec -p "$PROFILE" -- "${app[@]}" "$@"
fi

export SOVANDER_CONFINEMENT=none
exec "${app[@]}" "$@"
