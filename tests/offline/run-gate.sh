#!/usr/bin/env bash
# Offline release gate (Requirements 9.1, 9.7, 9.8), run by the CI "offline" job after the .deb is
# installed on a runner with AppArmor. Fails when the promise is not kept by the system itself:
#   1. "deny network inet," and "deny network inet6," are in both profile blocks, in the source and
#      in what postinst installed (a plain text check that no test skip can hide)
#   2. the kernel has the profile and its bwrap child loaded in enforce mode (needs sudo to read)
#   3. aa-exec can enter it, as the launcher does
#   4. tests/offline: a confined probe reports VERIFIED, its child is denied, AF_UNIX works, the engine
#      converts over its pipe, a canary on 127.0.0.1 sees no connection; and the unconfined control
#      sees one. Plus the launcher test, which must now report "apparmor".
#
#   tests/offline/run-gate.sh             full gate (sudo, installed .deb)
#   tests/offline/run-gate.sh --static    step 1 on the source profile only (no sudo, any machine)
set -euo pipefail

repo=$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)
source_profile=$repo/packaging/apparmor/sovander
installed_profile=/etc/apparmor.d/sovander

fail() {
    echo "offline gate: FAIL: $*" >&2
    exit 1
}

# Lines that are rules, with the comment part dropped ("#abi4# " lines stay comments here: they never
# carry a network rule, and the per-block check in tests/offline reads the installed variant)
deny_count() { # file rule -> how many rule lines equal it
    sed -e 's/#.*//' -e 's/^[[:space:]]*//' -e 's/[[:space:]]*$//' "$1" | grep -cxF "$2" || true
}

check_static() {
    local file=$1 blocks rule n
    [[ -f $file ]] || fail "$file is missing"
    blocks=$(grep -cE '^[[:space:]]*profile[[:space:]]' "$file" || true)
    ((blocks == 2)) || fail "$file: expected the sovander profile and its bwrap child, found $blocks blocks"
    for rule in "deny network inet," "deny network inet6,"; do
        n=$(deny_count "$file" "$rule")
        ((n >= blocks)) || fail "$file: '$rule' in $n of $blocks profile blocks"
    done
    # An allow rule for an internet family would be overridden by the deny, but must not exist at all
    if sed -e 's/#.*//' "$file" | grep -nE '^[[:space:]]*((allow|audit)[[:space:]]+)*network[[:space:]]*(inet6?([[:space:]]|,)|,|(tcp|udp|raw)\b)'; then
        fail "$file allows an internet socket"
    fi
    echo "offline gate: $file denies inet and inet6 in all $blocks blocks"
}

check_static "$source_profile"
if [[ ${1:-} == --static ]]; then
    exit 0
fi

check_static "$installed_profile"

loaded=$(sudo cat /sys/kernel/security/apparmor/profiles)
grep -qxF 'sovander (enforce)' <<<"$loaded" || fail "profile 'sovander' is not loaded in enforce mode"
grep -qxF 'sovander//bwrap (enforce)' <<<"$loaded" || fail "child profile 'sovander//bwrap' is not loaded in enforce mode"
echo "offline gate: sovander and sovander//bwrap loaded in enforce mode"

aa-exec -p sovander -- /bin/true || fail "aa-exec cannot enter the sovander profile"
echo "offline gate: kernel.apparmor_restrict_unprivileged_unconfined=$(cat /proc/sys/kernel/apparmor_restrict_unprivileged_unconfined 2>/dev/null || echo n/a)"

cd "$repo"
SOVANDER_REQUIRE_CONFINEMENT=1 SOVANDER_PROBE_LIBDIR=/usr/lib/sovander \
    python3 -m pytest -v -rs -p no:cacheprovider tests/offline \
    "tests/packaging/test_packaging.py::test_launcher_confines_exactly_when_the_profile_is_loaded"
echo "offline gate: passed"
