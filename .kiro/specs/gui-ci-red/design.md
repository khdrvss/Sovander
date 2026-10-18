# Design: GUI tests red on Ubuntu 24.04

## Root cause analysis

### B1: printing needs PyGObject's cairo bridge, which nothing declares

`sovander/print.py` draws every page through `context.get_cairo_context()` (and PangoCairo for
text). PyGObject turns the C `cairo_t` into a Python `cairo.Context` through `gi._gi_cairo`, shipped
in `python3-gi-cairo`. `python3-gi` neither depends on nor recommends it, and the package does not
list it. On the development machine only other applications pulled it in (`apt-cache rdepends --installed`).

- Reproduced: blocking `gi._gi_cairo` makes `test_each_page_prints_on_paper_of_its_own_size` and
  `test_text_is_wrapped_and_paginated` fail with CI's exact error.

### B2: Ubuntu 24.04 demoted libheif's HEVC decoder to Suggests

`heif-gdk-pixbuf` registers the gdk-pixbuf "heif" loader, but decoding HEVC needs libheif's
plugin `libheif-plugin-libde265`. libheif 1.17.6-1ubuntu4 depended on it; 1.17.6-1ubuntu4.3
(Feb 2026, LP: #2142762) made it a Suggests, to keep an H.265 decoder off Ubuntu's ISO. A fresh
system now has the loader without the codec: `Unsupported feature: Unsupported codec`, no pixbuf,
DamagedImage. This machine kept the plugin from the older version.

Ubuntu 22.04's libheif1 1.12 has no plugin system: it links `libde265-0` directly
(packages.ubuntu.com/jammy/libheif1), and there is no `libheif-plugin-libde265` package there.

### B3: the page range test cannot pick "Print to File"

`test_only_the_pages_picked_are_drawn` printed with `PRINT` (no dialog), naming GTK's "Print to
File" printer, and expected only page 2 drawn. GTK 4.14.5 never selects that printer this way:
`find_printer`'s `printer_added_cb` returns early for virtual printers ("FIXME this skips 'Print to
PDF' - is this intentional ?"), then falls back to the default printer, or the first one listed.

| Environment | What GTK did | Seen as |
|---|---|---|
| development machine (CUPS with printer queues) | sent the job to a printer queue, drew page 2 | pass; 41 "Sovander" jobs queued on a disabled printer |
| fresh 24.04 container, CUPS without printers | no printer: `cancel`, nothing drawn | `assert [] == [1]` |
| GitHub runner, no CUPS | waits for CUPS's printer list (still waiting after 75 s, `cancel()` does not end it) | timeout, then the memory tests fail |

In the last case the operation stays in `print._running` and in GTK; its `draw-page` handler is a
bound method of `_PdfJob`, which holds the PageView. The view's i18n listener outlives the test, and
the memory tests that run later fail on it: the CI report shows the print test's PageView
(`0x7f3fb0724b80`) in both memory failures. `CUPS_SERVER=/nonexistent` reproduces exactly these three.

No printer-free GTK run can test page ranges: `EXPORT` (`run_pdf`) sets `GTK_PRINT_PAGES_ALL` and
drops the ranges. Choosing the pages is GTK's job; Sovander's is to draw each page GTK asks for.
The app always prints through the dialog (`window.py`), which is not affected.

### B4: three hand-kept package lists

Runtime dependencies live in `packaging/debian/control`; CI and the release workflow keep their own
lists. C16 added the SVG and WebP loaders to the first two, not to `release.yml`. A mechanical
comparison finds exactly those two missing there and nothing missing in `ci.yml`.

## Bug conditions and postconditions

| Bug | C (input and state) | P (fixed, observable) |
|---|---|---|
| B1 | Depends only, no `python3-gi-cairo`; any view printed | `python3-gi-cairo` in Depends; printed PDF has the red box and every line |
| B2 | Ubuntu 24.04, libheif1 ≥ 1.17.6-1ubuntu4.3, Depends only; HEVC HEIC | decoder in Depends; `photo.heic` decodes to 160 × 96 |
| B3 | any print service state: none, empty, or real printers | print tests pass the same everywhere, send no job, leave no view alive |
| B4 | a runtime dependency missing from a test environment | every Depends entry installed by every CI and release test environment |

Fix property `C ⇒ P`; preservation `¬C ⇒ unchanged`: with gi-cairo and the plugin present (this
machine), every existing view, print and packaging test gives the same result.

## Fix

1. `packaging/debian/control` Depends: `python3-gi-cairo` and
   `libheif-plugin-libde265 | libheif1 (<< 1.15~)`. apt takes the plugin on 24.04 and the
   built-in decoder's libheif1 on 22.04; the offline bundle resolves the same way and installs with
   `--no-install-recommends`, which is why Recommends would not do. `tests/packaging` DEPENDS follows.
2. `ci.yml`: `python3-gi-cairo` in GUI_PACKAGES; the HEVC plugin through a gui matrix entry for
   24.04 only (22.04 has no such package).
3. `release.yml` TEST_PACKAGES: `python3-gi-cairo`, `libheif-plugin-libde265`, `librsvg2-common`,
   `webp-pixbuf-loader`.
4. `tests/gui/test_print.py`: the range test becomes `test_each_page_is_drawn_once_when_gtk_asks_for_it`:
   export a 3-page PDF, every page rendered once, in order. No printer, no print service.
5. `tests/packaging/test_packaging.py`: every Depends entry (one of its alternatives) appears in the
   test package lists of `ci.yml` and `release.yml`.

Cost for users: python3-gi-cairo (8 kB, with python3-cairo), the plugin and libde265.

### Rejected

- Recommends instead of Depends for the HEVC plugin: the offline bundle installs with
  `--no-install-recommends`, so its users would keep the bug.
- First attempt, tried and dropped: `cups-daemon` in CI plus a fixture freeing operations left
  unfinished. The fixture worked (memory tests green with the print test stuck), but the fresh
  container showed CUPS without printers cancels the job, and with printers the test prints on them.
- A CUPS queue to /dev/null in CI: covers GTK's ranges, not Sovander's code, and on a developer's
  machine the test would still go to a real printer.
- Installing CI's dependencies from `control` with `apt-get satisfy` (one list, no drift): the real
  cure for B4, but it changes every install step; the packaging test in (5) catches drift for now.
  Follow-up.

## Found along the way (outside this fix)

- GtkSourceView 5.12.0 and line numbers (C16): the leak is real. A first check here passed with
  line numbers forced on, but the repo's memory test watched only the outer TextView, which is always
  freed; watched from C, 0 of 200 closed views were freed. Corrected in
  `.kiro/specs/line-numbers-gate/` (gate at 5.21, test watching the view and buffer).
- 41 test jobs named "Sovander" are queued on the development machine's disabled printer; one other
  job there is not from the tests. Cancelling them is the owner's call.

## Properties to test

| Group | Check | Unfixed code |
|---|---|---|
| Bug exists | Depends list has `python3-gi-cairo` and the HEVC alternative (packaging test) | fails |
| Bug exists | every Depends entry in ci.yml and release.yml test lists (packaging test) | fails (both) |
| Bug exists | print and memory tests with `CUPS_SERVER=/nonexistent` | 3 fail |
| Fix works | the same, after the patch | pass |
| Fix works | fresh `ubuntu:24.04` container, release.yml's packages, `--no-install-recommends`, no CUPS: GUI tests | pass |
| Fix works | a full GUI run queues no print job (`lpstat -o` count before and after) | — |
| Preservation | full GUI suite and fast suite on this machine | pass before and after |
| Preservation | memory tests still fail when a view is really kept (a scratch plugin keeps every view) | fail before and after |
| Preservation | without gi-cairo the print tests still fail (B1 stays detectable) | fail before and after |
| Preservation | `.deb` dependencies resolve on 22.04 and 24.04 (`apt-get satisfy -s` in containers) | resolve |
