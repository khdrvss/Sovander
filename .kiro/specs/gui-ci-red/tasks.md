# Implementation Plan

- [x] 1. Bug-condition tests (red on the unfixed code)
  - `tests/packaging/test_packaging.py`: DEPENDS gains `python3-gi-cairo` and
    `libheif-plugin-libde265 | libheif1 (<< 1.15~)`; new test: every Depends entry is installed by the
    test package lists of `ci.yml` and `release.yml`
  - B3 condition runs: `CUPS_SERVER=/nonexistent` over test_print, test_sheet_view, test_text_view; the
    range test in a fresh ubuntu:24.04 container with CUPS and no printer
  - _Requirements: bugfix.md Expected 1, 3, 5, 7_
  - Acceptance: the packaging tests fail on the unfixed tree, naming the missing entries; the B3 runs fail
  - Факт: unfixed: 3 failed: control fields; ci.yml missing python3-gi-cairo and the HEVC entry;
    release.yml missing those two plus librsvg2-common and webp-pixbuf-loader. No CUPS: the range test and
    both memory tests failed, as in CI. Container with CUPS: `assert [] == [1]` (GTK cancelled: no printer)

- [x] 2. Preservation tests (green on the unfixed code)
  - full GUI suite and fast suite on this machine (gi-cairo, plugin and CUPS present)
  - mutation: a scratch plugin keeps every TextView and SheetView alive; the memory tests must fail
  - `apt-get satisfy -s` of the new Depends in clean ubuntu:22.04 and ubuntu:24.04 containers
  - _Requirements: bugfix.md Unchanged 1–6_
  - Acceptance: results recorded here as the baseline
  - Факт: GUI suite 332 passed (77 s); mutation: both memory tests fail; 22.04 resolves to libheif1
    1.12.0 + libde265-0 and python3-gi-cairo 3.42.1, 24.04 to libheif-plugin-libde265 1.17.6-1ubuntu4.9
    and python3-gi-cairo 3.48.2, no errors

- [x] 3. Patch
  - control Depends, tests/packaging DEPENDS, ci.yml (python3-gi-cairo, 24.04 HEVC entry), release.yml
    (four packages), test_print.py (the range test becomes a printer-free per-page check)
  - _Requirements: bugfix.md Expected 1–7_
  - Acceptance: task 1 checks green, task 2 checks unchanged; GUI tests green in a fresh ubuntu:24.04
    container with release.yml's packages; ruff clean; then push and CI `gui (ubuntu-24.04)` green
  - Факт: packaging 27 passed; no CUPS: 81 passed; GUI suite 332 passed, fast suite 712 passed;
    print jobs queued 42 before a full GUI run, 42 after; mutation: both memory tests still fail;
    without gi-cairo 2 print tests still fail; fresh ubuntu:24.04 container, release.yml's packages
    without LibreOffice, `--no-install-recommends`, no CUPS: 321 passed, 12 skipped (the skips CI shows);
    ruff and actionlint 1.7.12 clean. First attempt (cups-daemon in CI + a cleanup fixture) dropped, see
    design.md "Rejected". Pushed as 28d2e71 (history rewritten without co-author lines, owner's OK);
    CI run 39112432656: success, gui (ubuntu-24.04) green; 22.04 jobs red as before (non-blocking:
    engine crashes, GUI segfault in test_home.py)

## Progress

| Task | Status |
|---|---|
| 1 | done |
| 2 | done |
| 3 | done; CI green |
