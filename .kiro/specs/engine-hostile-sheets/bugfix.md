# Bugfix: a far-off cell makes a small spreadsheet cost minutes and gigabytes; a dead engine's proxy aborts Python

## Introduction

Found by the nightly fuzz on Ubuntu 22.04 ("engine/sheet: 2 of 10000 cases failed", run 39107134736)
and by CI's 22.04 engine job (exit 134, run 39116435681). Both also hold on Ubuntu 24.04. Root causes
reproduced in containers with CI's packages and on the development machine (24.04).

## Root cause

1. A fuzzed `budget.xls` (6 KB) with one cell record moved to row ~65282, column IV has a used area of
   65283 × 256 cells. Calc's HTML export writes every cell of that area (sc/source/filter/rtf/expbase.cxx):
   486 MB on 22.04, 1.64 GB on 24.04. `sheets.workbook` then parsed it with no limit: 501 s and 6.4 GB on
   24.04. A crafted variant (cell at row 16385) exports in 20.5 s, inside the engine watchdog, and parsed
   for 119.6 s at 1.6 GB.
2. A failed engine job's traceback keeps its frames, and with them UNO proxies. Once soffice is dead,
   `repr()` of such a proxy lets pyuno's DisposedException escape as a C++ exception: the process aborts
   (minimal repro on 24.04 and 22.04: kill soffice, `repr(doc)`). pytest's traceback rendering did that.

## Current Behavior (Defect)

WHEN a spreadsheet with one value far from the rest is opened
THEN the system parses an export of hundreds of megabytes or more for minutes, using gigabytes

WHEN an engine job fails and something renders its traceback's arguments after soffice died
THEN the whole process aborts

## Expected Behavior (Correct)

1. WHEN a spreadsheet's HTML export exceeds 128 MB
THEN the system SHALL refuse it with ConversionError ("This spreadsheet is too large to show.") without
parsing it
2. WHEN an engine job fails
THEN the system SHALL clear the job's frames before raising, so no UNO proxy outlives the job

## Unchanged Behavior (Regression Prevention)

1. WHEN ordinary workbooks are opened (largest corpus workbook: 75,770 cells, about 7.6 MB of HTML)
THEN the system SHALL CONTINUE TO show them as before (tests/engine, fidelity colours)
2. WHEN a job fails
THEN the system SHALL CONTINUE TO raise the same errors and restart the engine as before

## Evidence

- Cap: the crafted file is refused in 12.4 s, peak RSS 817 MiB (was 140 s, 1.6 GB); the full 22.04 sheet
  campaign: 10,000 cases, 0 failures (one case `ok` → ConversionError).
- New tests: `test_an_export_too_large_to_parse_is_refused`, `test_a_failed_job_leaves_no_proxies_behind`
  (fails without the fix: `work ['box','job','self'] == []`); 22.04 engine job 8 of 8 runs without abort.
- Open: the export itself still lands in memory before the cap applies (817 MiB peak); a cheap check of
  the used area before exporting would avoid it, but `gotoEndOfUsedArea` ignores formatted-only cells and
  can undercount. The cap value and the user-facing text are the owner's call.
