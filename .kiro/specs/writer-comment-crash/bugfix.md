# Bugfix: the engine aborts on any Writer document with a comment (Ubuntu 24.04)

## Introduction

The first nightly (run 39107134736) failed the Word fidelity floor on ubuntu-24.04: mean recall 90.2%
against 95%, with WordWithAttachments.docx and testComment.docx at 0 ("engine died during a job").
They are the only commented Word files in the corpus. On a fresh 24.04 with only Sovander's declared
dependencies, every document with a comment kills the engine. The development machine hid it: it has
the full `libreoffice-writer`.

## Root cause

`libreoffice-writer-nogui` 4:24.2.7 (Ubuntu 24.04) does not depend on `libreoffice-uiconfig-writer`,
unlike calc-, impress- and draw-nogui, which pull their uiconfig packages (`apt-cache show` of each).
Loading a document with a comment, Writer builds the comment's margin window from
`modules/swriter/ui/annotation.ui` (sw/source/uibase/docvw/AnnotationWin.cxx). The file is absent,
`NoSuchElementException` is thrown in a main-loop event, nothing catches it, and soffice aborts
(strace: ENOENT on annotation.ui, then SIGABRT). A plain soffice with a default profile does the same,
so Sovander's profile and options play no part. Upstream: tdf#155411 (headless still needs .ui files),
Debian #1028290 (the same gap for impress-nogui, fixed by a dependency).

## Current Behavior (Defect)

WHEN Sovander is installed from its declared dependencies on Ubuntu 24.04 and a DOCX, ODT or DOC with at
least one comment is opened
THEN the engine aborts and the document is not shown

## Expected Behavior (Correct)

1. WHEN Sovander is installed with apt or from the offline bundle on Ubuntu 24.04
THEN the package SHALL pull in `libreoffice-uiconfig-writer`
2. WHEN a Writer document with a comment is converted
THEN the engine SHALL stay running and show its pages
3. WHEN the CI, nightly and release jobs test the engine on 24.04
THEN they SHALL install `libreoffice-uiconfig-writer`, as users get it

## Unchanged Behavior (Regression Prevention)

1. WHEN Sovander is installed on Ubuntu 22.04 (LibreOffice 7.3, no uiconfig split)
THEN apt SHALL CONTINUE TO resolve the same package set (the alternative
`libreoffice-common (<< 4:7.6~)` is met there). Superseded for 22.04 by the extension below; 24.04's
set SHALL stay unchanged
2. WHEN documents without comments are converted
THEN their per-file fidelity SHALL CONTINUE TO be what it was (corpus sweep and fidelity suite)

## Fix and evidence

- Depends: `libreoffice-uiconfig-writer | libreoffice-common (<< 4:7.6~)`; tests/packaging DEPENDS;
  ci.yml engine, nightly.yml fidelity/fuzz-engine/perf (24.04 matrix entries), release.yml
  TEST_PACKAGES; README and the app's install command (`status_pages.INSTALL_COMMAND`).
- New test `test_a_comment_leaves_the_engine_running`: in a container with CI's 24.04 package set,
  `EngineCrashed` without the package, passed with it.
- Fidelity in the CI-like container: 90.2% before, 95.1% with the package; fresh 24.04 with the fixed
  .deb: Word 95.1%, slides 95.5%; corpus sweep through the installed app: no crash.
- apt simulations: 24.04 adds only uiconfig-writer (265 KB, 9 MB installed); 22.04 resolves as before.
- Not fixed here: 22.04, where only the full Writer carries the file; see the extension below. The Word
  floor passes with little margin (95.08%, 6 files under 90%).

## Extension (2026-10-18): Ubuntu 22.04

CI run 39118193450, engine (ubuntu-22.04): this test failed with `EngineCrashed` (1 failed, 50 passed,
20 skipped). LibreOffice 7.3's -nogui packages ship no `.ui` files: `annotation.ui` is only in the full
`libreoffice-writer`, which conflicts with `libreoffice-writer-nogui`. `libreoffice-uiconfig-writer`
exists on 22.04 only in jammy-backports (26.2), and Breaks 7.3's `libreoffice-common`. Every 22.04 user
lost every commented document. Hiding comments in the engine profile (Writer `Content/Display/Note`
false) does not help: the engine still aborts.

3. WHEN Sovander is installed on Ubuntu 22.04, with apt or from the offline bundle
THEN the package SHALL pull in the full `libreoffice-writer` instead of `libreoffice-writer-nogui`
4. WHEN `libreoffice-writer-nogui` 7.3 is already installed (Sovander 0.1.0, or by hand)
THEN installing Sovander SHALL replace it with `libreoffice-writer`, not fail

Fix:

- Depends: `libreoffice-uiconfig-writer | libreoffice-writer (<< 4:7.6~)`; `Breaks:
  libreoffice-writer-nogui (<< 4:7.6~)`, because apt 2.4 does not remove an installed package for a
  Depends ("held broken packages") but does for a Breaks. 24.04's packages are 4:24.2, so neither
  relation changes anything there.
- tests/packaging: DEPENDS and the Breaks field; ci.yml engine and nightly.yml fidelity/fuzz-engine/perf:
  the Writer moves out of ENGINE_PACKAGES into the matrix (22.04 `libreoffice-writer`, 24.04
  `libreoffice-writer-nogui libreoffice-uiconfig-writer`: apt cannot install both Writers); README.

Evidence (ubuntu:22.04 and ubuntu:24.04 containers, backports on and phased updates in, as on runners):

- apt simulations of the .deb, with and without Recommends: 24.04 fresh, with Sovander 0.1.0, with the
  full LibreOffice: every Inst/Remv line identical to main's .deb. 22.04 fresh: the only change is
  `libreoffice-writer-nogui` → `libreoffice-writer` (and `libreoffice-math-nogui` → `libreoffice-math`
  with Recommends): 346 packages without Recommends before and after. `libreoffice-core` is the full
  one on 22.04 already (python3-uno prefers it), so nothing else comes in. 22.04 with Sovander 0.1.0:
  without the Breaks apt fails (exit 100), with it removes writer-nogui and installs writer. 22.04 with
  the full LibreOffice: no change.
- Offline bundle dry run: 24.04 identical (431 packages); 22.04 416 → 416, writer-nogui swapped for
  writer. A real 22.04 bundle installed with `--network none` on a fresh 22.04 and over Sovander 0.1.0:
  exit 0, `apt-get check` and `dpkg --audit` clean, `annotation.ui` from libreoffice-writer, and the
  installed engine (production pipe and profile, no display) converts a commented ODT.
- Engine tests with CI's 22.04 set: main's set 1 failed (this test), 50 passed, 20 skipped, as in CI;
  the patched set (full Writer) 51 passed, 20 skipped; fuzz smoke and timings 4 passed both ways.
- Nightly fidelity on 22.04 (not blocking, informative): Word 90.0% → 94.9%, testComment.docx and
  WordWithAttachments.docx rendered again; still under the 95% floor (6 files under 90%, LibreOffice 7.3
  layout), slides 87.7% unchanged.
- The full Writer adds only Writer's own files (224 `.ui`, `libswuilo.so`, the `swriter` script,
  `lowriter`/`loweb`, a desktop entry, man pages): all under the paths the AppArmor profile already
  allows. The 22.04 offline gate already runs the full `libreoffice-core` (run 39118193450: passed).
  Not run: the offline gate with the full Writer (needs a 22.04 VM with AppArmor; CI's offline job does it).
- Side effect: the full Writer brings `libreoffice-writer.desktop`, and 22.04's /etc/gnome/defaults.list
  (desktop-file-utils) names it the default for .doc, .docx and .odt. A 22.04 desktop without
  LibreOffice (minimal installation) gets a "LibreOffice Writer" menu entry that opens Word files by
  default. A normal 22.04 desktop installation has the full Writer already (ubuntu-desktop recommends it).
- Not fixed: the app's install command (`status_pages.INSTALL_COMMAND`, shown only when the engine is
  missing) and README's 24.04 list name `libreoffice-uiconfig-writer`, which apt cannot install on 22.04
  (exit 100, it Breaks 7.3's libreoffice-common). README now says what 22.04 uses instead; the app's
  command is still the 24.04 one.
