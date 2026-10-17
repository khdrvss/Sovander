"""Fuzz the content router (Requirements 2.11, 7; NFR Robustness).

The router documents no exceptions: whatever the bytes, it returns a Kind. Families are the kinds
the router gives the pristine fixtures, so each format family gets its own share of cases.
"""

from collections import defaultdict

import pytest
from fuzzlib import FIXTURES, describe, iterations, run_campaign

from sovander import router

_FAMILIES: dict[str, list] = defaultdict(list)
for _path in sorted(FIXTURES.iterdir()):
    if _path.is_file():
        _FAMILIES[router.detect_path(_path).value].append(_path)


def route(data: bytes, name: str) -> str:
    kind = router.detect_bytes(data, name)
    if not isinstance(kind, router.Kind):
        raise TypeError(f"router returned {kind!r}")
    return kind.value


@pytest.mark.parametrize("family", sorted(_FAMILIES))
def test_router_survives_malformed_files(family):
    report = run_campaign("router", family, _FAMILIES[family], route, allowed=(), timeout=10,
                          cases=iterations())
    assert not report["failures"], describe(report)
