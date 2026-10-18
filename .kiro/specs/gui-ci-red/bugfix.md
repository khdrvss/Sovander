# Bugfix: GUI tests red on Ubuntu 24.04 (fresh-system dependencies, a print test that needs a printer)

## Introduction

The push CI run of 6883b2d failed only in `gui (ubuntu-24.04)`, the job that blocks the v0.1.0 tag:
6 failed, 315 passed. The failures come from four defects. Two of them reach users: a fresh Ubuntu
24.04 with only Sovander's declared dependencies prints blank pages and calls iPhone photos damaged.
The development machine hid both, because other applications had installed the missing packages
earlier, and libheif's HEVC decoder plugin was still a dependency when it was installed. A third
defect is a test that only ever passed by printing on this machine's real printer.

Evidence: CI run 39072263906, job 117512132789; reproduced locally (Ubuntu 24.04.5) by blocking
`gi._gi_cairo` and pointing `CUPS_SERVER` at nothing, and in a fresh ubuntu:24.04 container;
libheif changelog 1.17.6-1ubuntu4.3 and LP: #2142762 ("HEIC images ... will not decode on a ...
freshly installed system unless the user explicitly installs libheif-plugin-libde265"); GTK 4.14.5
`gtk/print/gtkprintoperation-unix.c` (`printer_added_cb` skips virtual printers) and
`gtkprintoperation.c` (`run_pdf` prints every page).

Approval: the owner's go-ahead on 2026-10-18 ("start doing things up") covers bugfix, design and
tasks in one pass; C, P and the root causes are reported back with the result.

## Current Behavior (Defect)

B1. WHEN a system has Sovander's declared dependencies but not `python3-gi-cairo` and the user prints a
page, text or image view
THEN the system raises `TypeError: Couldn't find foreign struct converter for 'cairo.Context'` in
every `draw-page` handler and the printed file has blank pages

B2. WHEN Sovander is installed on a fresh Ubuntu 24.04 (libheif1 1.17.6-1ubuntu4.3 or later, no
`libheif-plugin-libde265`) and the user opens an HEVC-coded HEIC photo
THEN the system shows "This image is damaged" (libheif: "Unsupported codec")

B3. WHEN `test_only_the_pages_picked_are_drawn` prints without the dialog to "Print to File"
THEN GTK never picks that printer (its lookup skips virtual printers) but the system's default one:
with no CUPS (GitHub's runners) it waits for good, and the stuck operation keeps the PageView that
`test_closed_view_is_freed` and `test_closed_view_is_freed_with_its_text` then report as a leak; with
CUPS and no printer it cancels and draws nothing; with a printer it sends the job there (41 such jobs
were queued on this machine's printer)

B4. WHEN the release workflow runs its tests before building a release
THEN the tests run without `librsvg2-common` and `webp-pixbuf-loader`, two of the package's runtime
dependencies, so the release tests check an environment users never have

## Expected Behavior (Correct)

1. WHEN Sovander is installed with apt on Ubuntu 22.04 or 24.04
THEN the package SHALL depend on `python3-gi-cairo`

2. WHEN a page, text or image view is printed on a system with only Sovander's declared dependencies
THEN the printed pages SHALL show the document's content

3. WHEN Sovander is installed with apt (or from the offline bundle) on Ubuntu 24.04
THEN the package SHALL pull in libheif's HEVC decoder (`libheif-plugin-libde265`)

4. WHEN Sovander is installed on Ubuntu 22.04, whose libheif1 (1.12) has the HEVC decoder built in
and no plugin package
THEN the dependency SHALL be satisfied by that libheif1, and the package SHALL CONTINUE TO install

5. WHEN the print tests run
THEN they SHALL need no printer and no print service, and SHALL send no job to any printer

6. WHEN the tests check how pages are drawn for printing
THEN they SHALL check Sovander's part: each page GTK asks for is drawn once (GTK picks the pages)

7. WHEN a runtime dependency is added to the package
THEN a packaging test SHALL fail until every CI and release test environment installs it

## Unchanged Behavior (Regression Prevention)

1. WHEN a PDF, text or image view is printed with `python3-gi-cairo` present
THEN the system SHALL CONTINUE TO produce the same pages (`tests/gui/test_print.py`: paper sizes,
current page, resolution limits, red box colours, wrapped text)

2. WHEN a PNG, JPEG, GIF, WebP, BMP, TIFF, SVG or AVIF image is opened
THEN the system SHALL CONTINUE TO decode it as before (`tests/gui/test_image_view.py`)

3. WHEN a closed sheet or text view is really kept alive by a reference
THEN `test_closed_view_is_freed` and `test_closed_view_is_freed_with_its_text` SHALL CONTINUE TO fail

4. WHEN the .deb is built and installed on Ubuntu 22.04 and 24.04 (CI `package` and `offline` jobs)
THEN it SHALL CONTINUE TO install and pass the packaging and offline tests

5. WHEN the user prints through the print dialog (the only way the app prints)
THEN the system SHALL CONTINUE TO print without any change to `sovander/print.py`; CUPS SHALL
CONTINUE TO be no package dependency

6. WHEN the text view is shown with GtkSourceView 5.12.0
THEN the system SHALL CONTINUE TO show no line numbers (C16's decision is outside this fix)
