"""Performance harness (NFR Performance, task 6.3): time to first page, written to build/perf/report.json.

On shared CI runners only generous ceilings are asserted, so a gross regression fails and a slow
runner does not. SOVANDER_PERF_REFERENCE=1 asserts the NFR targets instead: set it on the reference
machine, with the release build of the engine (the .deb LibreOffice, not the snap).
"""

import json
import os
import platform
import sys
import time
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
REPORT = ROOT / "build" / "perf" / "report.json"


def machine() -> dict:
    info = {"python": sys.version.split()[0], "cpus": os.cpu_count(), "platform": platform.platform()}
    try:
        info["os"] = platform.freedesktop_os_release().get("PRETTY_NAME")
    except OSError:
        pass
    try:
        meminfo = Path("/proc/meminfo").read_text(encoding="ascii").split()
        info["mem_total_mb"] = int(meminfo[meminfo.index("MemTotal:") + 1]) // 1024
    except (OSError, ValueError):
        pass
    return info


@pytest.fixture(scope="session")
def perf_report():
    results: dict = {}
    yield results
    if results:
        REPORT.parent.mkdir(parents=True, exist_ok=True)
        report = {
            "when": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
            "commit": os.environ.get("GITHUB_SHA"),
            "reference": os.environ.get("SOVANDER_PERF_REFERENCE") == "1",
            "machine": machine(),
            "results": results,
        }
        REPORT.write_text(json.dumps(report, indent=1) + "\n", encoding="utf-8")
        print(f"\nperf report: {REPORT}\n{json.dumps(results, indent=1)}")
