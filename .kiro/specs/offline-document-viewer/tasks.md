# Implementation Plan

Waves run in order; tasks marked (P) inside a wave run in parallel and own disjoint files.
Wave A: 1.1. Wave B: 1.2–1.4, 2.x, 3.x. Wave C: 4.x. Wave D: 5.x. Wave E: 6.x.

- [x] 1. Foundation
- [x] 1.1 Project skeleton and shared paths
  - Факт: `ruff check .` clean, `pytest` 2 passed (tests/unit/test_paths.py), 99 Gander fixtures in tests/fixtures/
  - `pyproject.toml` (ruff, pytest markers `engine`, `gui`), `sovander/__init__.py`, `sovander/paths.py` (XDG data/cache dirs, `SOVANDER_HOME` override for tests)
  - `scripts/dev-env.sh` (snap LibreOffice + pyuno for development), `.gitignore`, `README.md`, `tests/fixtures/` (Gander fixtures, MIT)
  - Done when `ruff check` and `pytest` run green on the empty suite
  - _Requirements: 13.1_
- [x] 1.2 Store for recents, positions and settings (P)
  - Факт: JSON Lines per entry (one bad line costs one entry), atomic writes; tests/unit/test_store.py green (82 unit tests across 1.2–1.4)
  - `sovander/store.py`: JSON with atomic writes, caps (50 recents, 500 positions), position key = sha256(first 64 KB) + size, missing-file marking, history-off mode
  - `tests/unit/test_store.py`
  - _Requirements: 11.1, 11.2, 11.3, 11.4, 11.5_
- [x] 1.3 Translations (P)
  - Факт: 64 strings en+ru, in-process .mo compiler (no msgfmt on dev machine), completeness test green
  - `sovander/i18n.py` (gettext domain `sovander`, locale fallback to English, runtime switch), `po/sovander.pot`, `po/ru.po`
  - `tests/unit/test_i18n.py`: every msgid translated in ru.po
  - _Requirements: 14.1, 14.2, 14.3, 14.5_
- [x] 1.4 Offline self-check (P)
  - Факт: probe only creates and closes inet/inet6 TCP+UDP sockets (no bind/connect/send, test-enforced)
  - `sovander/offline.py`: AF_INET/AF_INET6 socket probe, status enum (verified / unprotected / unknown)
  - `tests/unit/test_offline.py`
  - _Requirements: 9.1, 9.6, 9.7_

- [x] 2. Routing and archives
- [x] 2.1 Content router (P)
  - Факт: every fixture routes right from the 64 KB head, plus 21 misnamed copies; 397 tests green across 2.1–2.3
  - `sovander/router.py`: magic bytes, zip member names (OOXML/ODF/EPUB), OLE compound file (.doc/.xls/.ppt), PDF, images, text heuristic (port of Gander `ReadsAsText`); content wins over extension
  - `tests/unit/test_router.py` over `tests/fixtures/`
  - _Requirements: 2.1–2.11, 7.1, 7.2_
- [x] 2.2 Zip name decoding (P)
  - Факт: cp866/cp1251 added with Russian rules; gap: Korean/Big5 not auto-detected (manual pick works)
  - `sovander/zipnames.py`: port of Gander `ZipNames.kt` (UTF-8 flag, cp437, Shift-JIS, GBK) plus cp866 and cp1251; manual override
  - `tests/unit/test_zipnames.py` with Gander's `names-*.zip` fixtures
  - _Requirements: 8.5, 8.6_
- [x] 2.3 Archive reading (P)
  - Факт: own bounded reader (zipfile's unbounded bzip2/LZMA inflate rejected as a zip-bomb risk); AES checked byte-for-byte against 7-Zip archives; gaps: ZipCrypto ~1 MB/s, AES path not run on Python 3.10, Deflate64 → Unsupported
  - `sovander/archive.py`: listing as folders, entry bytes in memory, ZipCrypto, WinZip AES (AE-1/AE-2) via `cryptography`, caps (200,000 entries, 2 GB expanded), wrong-password errors
  - `tests/unit/test_archive.py`
  - _Requirements: 8.1, 8.2, 8.3, 8.4, 10.4_

- [x] 3. Document engine and cache
- [x] 3.1 Engine process
  - Факт: production pipe `sovander-<uid>`; snap's private /tmp makes the pipe unreachable in dev, so dev uses SOVANDER_ENGINE_CONNECT=socket (auto free port); LibreOffice fetched remote images despite link settings → network content providers disabled in engine-profile.xcu, canary test proves 0 requests; 52 engine tests green (59 s)
  - `sovander/engine/process.py`: start headless LibreOffice on `pipe,name=sovander-<uid>`, private profile seeded from `data/engine-profile.xcu`, readiness wait, watchdog, kill and restart
  - `data/engine-profile.xcu`: no update check, no online features, no remote content
  - `tests/engine/test_process.py` (marker `engine`)
  - _Requirements: 9.1, 10.5_
- [x] 3.2 Engine conversions
  - Факт: in-memory in and out (private:stream); warm page 1 / full: 0.43/0.50 s (3-page docx), 2.20/3.05 s (20 pages), 2.65/7.53 s (heavy deck); gaps: bug53475 password docx reads as damaged, deep-data.xlsx fails as in the spike
  - `sovander/engine/client.py`: load from path or bytes (InputStream), Hidden/ReadOnly/no macros/no link updates, password, PDF export page 1 first then the rest, 60 s job timeout
  - `tests/engine/test_client.py` over fixtures and the spike corpus
  - _Requirements: 2.2, 2.3, 2.5, 2.10, 3.6, 3.8, 3.9, 3.10, 5.7, 10.1_
- [x] 3.3 Spreadsheet grid export
  - Факт: Grid with fills, spans, widths, hidden rows/cols dropped, formulas on demand; RED vs Req 4.6: 66,935-cell workbook takes 23.2 s (engine load 20.0 s, snap build under memory pressure) → see 4.8
  - `sovander/engine/sheets.py`: per-sheet HTML export, parser to `Grid` (text as shown, fills, spans, widths), formula of one cell on demand
  - `tests/engine/test_sheets.py`: colours survive (spike workbooks), values not recalculated
  - _Requirements: 2.4, 4.1, 4.2, 4.3, 4.4, 4.5, 4.6_
- [x] 3.4 Macro detection (P)
  - Факт: OOXML/ODF/OLE parts detected, stdlib only; gaps: .ppt VBA and XLM macros in .xls not detected
  - `sovander/engine/macros.py`: `vbaProject.bin`, ODF `Basic/`, OLE `Macros`/`_VBA_PROJECT` streams
  - `tests/unit/test_macros.py`
  - _Requirements: 10.1, 10.2_
- [x] 3.5 Render cache (P)
  - Факт: content + engine version + font-set keys, atomic puts, 1 GB LRU, memory-only mode; 44 unit tests green across 3.4–3.5
  - `sovander/cache.py`: key = sha256(content) + engine version + font-set hash, LRU 1 GB, partial entries, purge per document, memory-only mode
  - `tests/unit/test_cache.py`
  - _Requirements: 9.10, 11.3, 11.5_

- [x] 4. Views
- [x] 4.1 Page view
  - Факт: 756-page PDF laid out in 0.08–0.10 s, page 1 texture in 0.11–0.12 s (target 1.5 s); textures capped (peak 82 MB at 400%); render.py split out to keep files < 500 lines; 22 gui tests green; gap: page sizes read upfront (~3.7 s at the 50,000-page cap)
  - `sovander/views/page_view.py`: MuPDF pages to textures on a worker thread, lazy visible pages, zoom (keys, Ctrl+scroll, pinch), fit width/page, page counter, go to page, progressive pages
  - `tests/gui/test_page_view.py` (marker `gui`, Xvfb)
  - _Requirements: 2.1, 3.1, 3.2, 3.3, 3.4, 5.1, 5.2, 5.3, 5.7_
- [x] 4.2 Selection, links and link dialog
  - Факт: web links open only through the portal OpenURI over D-Bus (a child process would inherit the no-network profile); only http/https/mailto openable; punycode host, escaped invisible/bidi chars, user@host warning; tests/gui/test_links.py renamed test_links_gui.py (basename clash with the unit test); gaps: internal links land on the page top, no keyboard link navigation, portal Response not awaited
  - selection/copy in `page_view.py`; `sovander/links.py`: internal jumps, web-link dialog (Open/Copy), portal OpenURI over D-Bus
  - _Requirements: 3.5, 3.11, 9.3, 9.4, 9.5_
- [x] 4.3 Night mode and presentation mode
  - Факт: lightness flip keeps hue (red stays red; invert_irect rejected: red→cyan), photos untouched; 15 ms/page text, 0.3–0.5 s coloured slide without numpy (21 ms with); 11 unit + presentation gui tests green
  - `sovander/views/night.py` (hue-keeping inversion, image boxes untouched), `sovander/views/present.py`
  - _Requirements: 5.4, 5.5, 12.2_
- [x] 4.4 Sheet view
  - Факт: custom-drawn grid (sheet_grid.py split out at 495 lines); 100,000×30 synthetic: first paint 67–79 ms, scroll step 18–23 ms (Xvfb, software rendering); 44 gui + 3 engine tests green; gaps: no alignment/font/text colour in the grid data (numbers right-aligned by guess), first search over all sheets runs on the main thread, no zoom or Ctrl+C, a real 3M-cell grid takes ~840 MB (→ 4.8)
  - `sovander/views/sheet_view.py`: virtualised grid, letters and numbers, tabs, fills/spans/widths, address + formula bar
  - _Requirements: 4.1, 4.3, 4.4, 4.5, 4.6_
- [x] 4.5 Text and image views (P)
  - Факт: 54 gui tests green under xvfb; oversized-image guard (20k×20k PNG refused before allocating); GtkSourceView path untested here (not installed); `_weak` helper duplicated in both views → move to views/__init__.py in 5.1
  - `sovander/views/text_view.py` (GtkSourceView 5, plain TextView fallback, encoding detection, 5 MB Show more), `sovander/views/image_view.py` (zoom, animation)
  - _Requirements: 2.7, 2.9, 7.1, 7.3_
- [x] 4.6 Web view for books and Markdown (P)
  - Факт: content filter blocks every non sovander-asset/data/blob URL before any request; a 127.0.0.1 canary gets 0 connections from ~20 hostile Markdown/EPUB fetch tricks (a plain WebKit view does connect); injected script/onerror/javascript: never run; 35 gui tests green ×5; gaps: in-page words ("Contents", "Next") English only, WebKit bwrap sandbox vs AppArmor unproven until 6.2
  - `sovander/views/web_view.py`, `data/assets/` from Gander (epub, md, marked, DOMPurify, CSP pages), `sovander-asset://` scheme, every other request refused
  - _Requirements: 2.6, 2.8, 9.2_
- [x] 4.8 Fast first screen for large spreadsheets
  - measure the engine's load time for large workbooks on a non-snap engine first; if Req 4.6 is still missed, show values from a streaming reader at once and swap in the engine's formatted grid when it is ready
  - _Requirements: 4.6_
  - Факт: sovander/sheets_fast.py (stdlib zipfile + expat; openpyxl measured 135k vs 471k cells/s and would be a new universe Depends) shows the first 1000 rows of each visible sheet with "Formatting…" until the engine grid replaces it, keeping sheet/selection/scroll. Preview built in: 150,000×20 sheet 0.13 s (before: 25.3 s engine load, then killed by the 60 s watchdog, error page); 66,935 cells 0.22 s (before 24.8 s); CVLKRA 0.14 s (before 4.27 s). Engine failure → note instead of an error page. Gaps → C7. By owner's order only targeted tests (unit + sheet gui + engine sheets) were run by the agent; full non-engine suite run afterwards: 993 passed
- [x] 4.7 Find
  - Факт: own matching over page characters (MuPDF search_for ignores case only for Latin: "привет" missed "Привет"); protocol find(query, forward, case) -> (index, total) + clear() + optional watch(); 74 gui tests green; gap: text-view highlights capped at 10,000 (count stays complete)
  - `sovander/find.py`: one bar, backends for page, sheet, web and text views, "n of m", Enter/Shift+Enter, case toggle
  - _Requirements: 6.1, 6.2, 6.3, 6.4, 6.5, 6.6_

- [x] 5. App shell
- [x] 5.1 Application and windows
  - Факт: app/window/open pipeline (+ open_jobs.py, document_context.py, status_pages.py split for size); launch → first page: PDF 0.28 s warm / 0.77 s while the engine starts, docx 1.39 s warm, 7.78 s cold on the snap — on Ubuntu's own .deb LibreOffice 24.2.7 the cold launch → page 1 is 1.48–1.77 s (first launch ever 1.77 s, 1.96–2.12 s with a cold disk cache): 5 s target met; engine readiness poll 200 → 20 ms; `scripts/dev-env.sh --fetch-deb` + `SOVANDER_DEV_ENGINE=deb` measure the product engine without root, 0.69 s from the render cache; real launch screenshotted by hand: docx, xlsx (tabs, fills, formula bar), pptx, home screen all render; clean exit on SIGINT/SIGTERM/SIGHUP
  - `sovander/app.py`, `sovander/__main__.py`: open from file manager/CLI/drop/chooser, window per document, header bar, F11, theme, error messages, engine warm-up at start
  - _Requirements: 1.1, 1.2, 1.3, 1.4, 1.6, 12.1, 12.3, 12.4_
- [x] 5.2 Home screen and resume
  - Факт: home.py + thumbs.py (page 1 of PDFs and cached renders, images; none when history is off; deleted via the store's forget hook); resume wiring lives in 5.1; freed-after-close tests catch an injected strong listener
  - recents with thumbnails, missing marks, remove/clear, reopen at saved position
  - _Requirements: 11.1, 11.2, 11.3, 11.4_
- [x] 5.3 About, offline status, language, print
  - Факт: about.py (Gtk.AboutDialog: libadwaita 1.1 has no AboutWindow), print.py (pages at 150–600 dpi, web/text/image; sheets NotPrintable), Language submenu (System/English/Русский) saved in settings and applied to every window — added after the shell shipped without a way to pick the language
  - About with offline status and licences, language switch rebuilding windows, `sovander/print.py`
  - _Requirements: 9.7, 12.5, 14.4_

- [x] 5.4 Archive browser view
  - Факт: folders first, Up/Alt+Up/Backspace, entries read on a thread into memory, password retry (last good password tried first), encoding menu; 55 gui tests green across home/about/print/archive ×3 runs
  - `sovander/views/archive_view.py`: folder-like list over `archive.Archive`, entry opens through the router from bytes, password prompt, file-name encoding picker
  - _Requirements: 8.1, 8.2, 8.4, 8.6_

- [x] 6. Packaging and validation
- [x] 6.1 Desktop integration and packages
  - Факт: .deb 194 KB (708 KB installed); 24 deps verified on 22.04 and 24.04; launcher uses `python3 -I` + runpy (a decoy ./sovander/ was shown to hijack `python3 -m`); AppArmor source for abi 3.0 + generated abi 4.0 variant with `userns` for WebKit's bwrap, compiled with parsers 3.0.4 and 4.0.1 but never loaded (needs root); offline bundle dry run: 423 packages ≈ 298 MB; 25 packaging tests green; Maintainer set to Saidxon Xaydarov <saidxon3105@gmail.com> (owner, 2026-10-17); open: homepage URL missing (metainfo url-homepage-missing)
  - `data/sovander.desktop` (MIME list), `packaging/sovander.sh`, `packaging/apparmor/sovander`, `packaging/debian/`, `packaging/make-offline-bundle.sh`
  - _Requirements: 1.5, 9.7, 13.1, 13.2, 13.3, 13.4_
- [x] 6.2 CI and offline gate
  - Факт: ci.yml (lint, unit, gui, engine, package, offline) on 22.04 + 24.04 runners, actions pinned by SHA, contents: read; run-gate.sh --static fails on 4 broken profile copies; validated with yaml + actionlint only — NEVER RUN ON GITHUB, the package/offline jobs need sudo; ubuntu-22.04 runner retires 2027-04-17
  - `.github/workflows/ci.yml`: Ubuntu 22.04 + 24.04, unit/engine/gui tests, offline job loading the profile, gate on `deny network`
  - _Requirements: 9.6, 9.8, 13.1_
- [x] 6.3 Fidelity, fuzz and performance harnesses
  - Факт: fidelity floors from the spike (Word 95.8% ≥ 95%, slides 95.25% ≥ 95% — tight, colours 14/14); fuzz 160,000 in-process + ~1,190 engine cases, 0 crashes/hangs; perf: warm 20-page first page 1.04–2.06 s, cold 8.35 s (snap), 756-page PDF 0.08–0.09 s
  - `tests/fidelity/` (from `spike/compare.py`), `tests/fuzz/`, `tests/perf/`
  - _Requirements: 10.3, 10.4, 10.5_
- [x] 6.4 Complete Russian translation
  - Факт: web view's own words (20, views/web_words.py) reach the page as a WebKit user script (CSP unchanged) and switch with i18n; tests/gui/test_web_view_words.py; gap: error details from WebKit itself stay English. By owner's order only targeted tests were run (no full suite)
  - all strings from 5.x in `po/ru.po`, completeness test green
  - _Requirements: 14.1, 14.5_

## Cascade (fixes found during integration)

- [x] C1 Closed views leaked: PageView handlers, the render thread's callback and presentation jobs held every closed document in memory
  - Факт: shared `views.weak_handler` replaces three copies of `_weak`; PageView/present connect through it; `_Slide.do_unroot` stops its worker; tests/gui/test_view_lifetime.py fails with the bug and passes with the fix; full suite 743 passed

- [x] C2 PageView's do_dispose never ran (PyGObject skips Python dispose once the wrapper is gone): every closed PDF left a render thread and a "children left" warning
  - Факт: thread stopped by weakref.finalize; a first try that also unparented the loading row from the finalizer segfaulted (GTK had freed the parent), so the row is now drawn text, not a child widget; render threads after 5 closed documents 5 → 0, warnings 17 → 0; test_view_lifetime + rewritten loading-row test (fails when the row is not drawn); full suite 832 passed

- [x] C3 GTK abort: Python's cycle collector ran in the thumbnail worker and finalised GTK widgets off the main thread (SIGABRT in the full suite, exposed once C1/C2 made views collectable)
  - Факт: views.collect_garbage_on_main_thread() (gc off, collect every 5 s on the GLib loop) called by app.run; tests mirror it in tests/conftest.py; full suite 3× green, no abort
- [x] C4 Slides opened at printed size and were cut off on the right (seen in the end-to-end screenshot)
  - Факт: slides open fit-page; asserted in test_slides_offer_presentation_mode
- [x] C5 Engine test fixture (tests/engine/conftest.py): production pipe name can attach to a running Sovander on a dev machine; silent skip when the engine fails to start; session engine overlaps the fidelity suite's when run together
  - Факт: shared `engine` fixture in tests/conftest.py, own pipe/socket per session, fails when an installed engine won't start; combined engine+fidelity run not executed (owner's order)
- [x] C6 Home screen cards have uneven heights (a tall document thumbnail stretches its card), seen in the end-to-end screenshot
  - Факт: fixed 148×180 thumbnail box; test fails on the old code (322 vs 246 px)

- [x] C7 Large-sheet follow-ups (from 4.8)
  - fast preview for .xlsx only: .ods/.xls/.xlsb/.csv still wait for the engine; preview lacks theme/indexed fill colours, merges and shared-formula dependents; only 1000 rows reachable if the engine fails (no progressive fill)
  - Grid storage is ~575 B/cell (3M cells ≈ 1.7 GB, NFR is 1 GB): compact per-row storage not done
  - _Requirements: 4.6, NFR memory_
  - Факт: instant preview now also for .csv/.tsv (sniffed separator, UTF-8 else cp1251; 150k×20 CSV 36 ms) and .ods/.fods (sovander/sheets_fast_odf.py, capped repeats; 150k×20 ODS ~0.95 s, FODS ~0.6 s) through sheets_fast.first_screen. Engine Grid storage: CellStore (int keys, plain text for unstyled cells, shared fills, Cell built on read) 241 → 123 B/cell on a generated 300k-cell grid (3M ≈ 0.37 GB); export HTML freed after parsing. Fast suite 1001 passed; test_100k_rows_first_paint_and_scrolling_are_fast failed once under heavy unrelated CPU load (~10 cores busy), passed when rerun alone (10 s). Gaps: build peak still 326 B/cell (parser dicts); ODS preview without formulas/widths/fills; .xls/.xlsb engine-only; no 3M-cell run with a real file

- [x] C8 Fast preview vs crafted spreadsheets: the preview parses untrusted files in the app's own process (before 4.8 only LibreOffice did, in its own)
  - `<c r="ZZZZZZZZZZ1">` built a column list of 1.5×10¹⁴ entries (hang / out of memory), `<row r="999999999999">` the same for rows, `ht="inf"` raised OverflowError (error page instead of the document), shared strings/styles were read whole (zip bomb), an ODS `<text:s text:c="2000000000"/>` built a 2 GB string; any preview error stopped the whole open
  - _Requirements: 10.4, 10.6_
  - Факт: sheets_fast.py bounds (MAX_COL 16384, MAX_ROW 1,048,576, MAX_TEXT 32,767 per cell, MAX_PART 64 MB checked against the declared size, which zipfile never inflates past), OverflowError caught, sheet_job keeps opening through the engine when the preview fails; sheets_fast_odf.py caps text and space runs. Reproduced before the fix (column index 146813779479509, OverflowError); tests: test_crafted_coordinates_sizes_and_text_stay_bounded, test_parts_larger_than_the_limit_are_left_to_the_engine, test_a_failing_preview_never_stops_the_engine_opening_the_file, test_crafted_ods_cell_text_stays_bounded
- [x] C9 CSV with ';' between fields and ',' in numbers was split at the commas, in the preview and the engine alike (seen in the end-to-end screenshot of a Russian Excel CSV)
  - csv.Sniffer prefers ',' on a tie; the preview also decoded UTF-16 as cp1251 and Western cp1252 as cp1251
  - _Requirements: 4.7, 4.2_
  - Факт: engine.client.csv_separator / csv_encoding shared by csv_options and the preview (tab when it fits; most fields; ';' before ',' before '|'; decimal or prose commas lose); 8 separator and 4 encoding cases in tests/unit/test_sheets_fast_odf.py, engine test_semicolon_csv_with_decimal_commas; the old Sniffer picks ',' for the screenshot's file
- [x] C10 Text cut off at the bottom in rows shorter than a line (a 19 px line of the UI font in LibreOffice's 17 px default row; seen in the end-to-end screenshot of an .ods)
  - _Requirements: 4.8, 4.4_
  - Факт: sheet_grid bottom-aligns cell text as spreadsheets do; tests/gui/test_sheet_grid_text.py counts ink rows of "gyp" in a 15 px and a 60 px row: 12 vs 12 now, 7 vs 12 with the old line (fails)
- [x] C11 The engine outlived an app that ended without its atexit (GTK exits with code 1 when the display goes away; also a crash or SIGKILL): reproduced end to end, LibreOffice kept running
  - _Requirements: 10.7_
  - Факт: engine/process.py starts a guard per engine (`sh` reading a pipe only the app writes; EOF → kill -9 of the engine's process group; a normal stop writes "keep"); allowed by the AppArmor profile (sh ix, signal peer=sovander). The first version's `kill -KILL --` is rejected by dash and was hidden by 2>/dev/null — caught by the end-to-end test, fixed to `kill -9`; tests/unit/test_process_guard.py (stand-in engine, fails on the old syntax), tests/engine/test_engine_guard.py (a real engine and an app that os._exit()s)

- [x] C12 A CSV's sheet tab was an empty button once the full sheet arrived (seen in the end-to-end screenshot)
  - the file reaches LibreOffice as a stream without a name, and LibreOffice names a CSV's sheet after its file: the engine returned ['']
  - _Requirements: 4.1_
  - Факт: sheets_fast.file_sheet_name names it after the file for the preview and the full sheet alike; test_an_unnamed_engine_sheet_takes_the_file_name; the relaunched app shows the tab "goroda"

- [x] C13 Development moved from the snap LibreOffice 26.2.5 to Ubuntu's own 24.2.7 (the engine the .deb depends on), at the owner's request
  - _Requirements: NFR Performance, 6.3_
  - Факт: scripts/dev-env.sh uses the system LibreOffice when installed (production pipe, no socket workaround). On it: engine + fidelity + fuzz + perf 75 passed, 1 skipped (AppArmor gate) in 60 s (snap: 4.5 min); engine start 0.84–1.11 s, word-pages.docx launch → page 1 1.77 s (first launch 2.08 s), drawing.docx 1.92 s. Fidelity: Word mean 95.1%, 6 files under 90% (26.2: 5) — the sixth is Bug54771b.docx (5 words; 24.2 leaves out a title in a content control), floor set to 6 for the engine we ship. test_snap_engine_refuses_the_unreachable_pipe no longer needs the snap installed.

- [x] C14 Never tested on the PyMuPDF the .deb installs: development used pip's 1.28.2 from ~/.local, the package depends on Ubuntu's python3-fitz 1.23.7 (found before the first install)
  - on 1.23.7: 5 unit/gui + 18 engine tests failed. Product bugs: (a) thumbs.pdf_png passed PdfChunk (a bytes subclass) to fitz.open, which 1.23.7 rejects — every office file would have had no thumbnail, swallowed as "no PDF thumbnail"; (b) 1.23.7 reads a /S/URI action of an unknown scheme (javascript:, smb:) as a launch with no file, so such links did nothing instead of going to the confirmation dialog (9.x). Test-only: helpers that dropped the document and kept its page ("orphaned object" in < 1.24)
  - _Requirements: 11.x thumbnails, 9.4 links, 13.1_
  - Факт: thumbs passes bytes(pdf); links._with_uri reads the address from the action (xref_get_key "A/URI"); test_a_pdf_from_the_engine_gets_a_thumbnail; helpers keep their documents. Ubuntu's PyMuPDF simulated without root (apt-get download python3-fitz libmujs3 libgumbo2 → dpkg -x into build/pymupdf-deb, first on PYTHONPATH, a stub making `import pymupdf` fail as on Ubuntu). CI already installs python3-fitz from the archive; the gap was the local setup only

- [x] C15 The installed app crashed on every book and Markdown file, and printing listed no printers: first run of the real .deb inside its AppArmor profile (2026-10-18)
  - WebKit's bwrap sandbox, in the profile's bwrap child, could not build its root: after pivot_root the kernel sees it as /newroot ("mkdir /newroot/usr/", "mknod /newroot/dev/null" denied) → "Failed to fully launch dbus-proxy" → abort. The print dialog's libcups was denied /run/cups/cups.sock. CI's offline job checked the network only, never WebKit or printing inside the profile
  - _Requirements: 9.1, 3.x (EPUB, Markdown), 12.x printing_
  - Факт: bwrap child: `/newroot/ rw, /newroot/** rwkl,` (exists only in bwrap's mount namespace); main: `/run/cups/cups.sock rw,`; both variants parse (apparmor_parser -Q -K -T); tests/offline test_web_view_sandbox_starts_inside_the_profile fails on the old profile ("Can't mkdir /usr") and runs in the CI offline job

## Progress

| Wave | Tasks | Status |
|---|---|---|
| A | 1.1 | done |
| B | 1.2–1.4, 2.1–2.3, 3.1–3.5 | done |
| C | 4.1–4.8 | done; 4.8 large .xlsx first screen 0.13–0.22 s (was 4–25 s or a watchdog error), follow-ups in C7 |
| D | 5.1–5.4 | done; app launches and renders docx/xlsx/pptx/PDF (screenshots, 2026-10-17) |
| E | 6.1–6.4 | done (CI never run on GitHub; AppArmor never loaded); cold start on Ubuntu's .deb engine 1.48–1.77 s (5 s target) |
| Cascade | C1–C15 | done; C8–C12 found in the final verification (retrospective, requirements 4.7, 4.8, 10.6, 10.7) |

Test suite (2026-10-17, final verification): ruff clean; unit + gui 1023 passed, 2 skipped; engine + fidelity + fuzz + perf 75 passed, 1 skipped (the AppArmor gate needs the installed .deb), then after C12 the open-pipeline, sheet and guard engine tests again (33 passed); six real launches (docx, xlsx, csv, ods, pptx, epub; Russian UI) exit 0 with no engine left, and none left after the display is lost; .deb builds (0.1.0, 882 KB).
