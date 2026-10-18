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
`libreoffice-common (<< 4:7.6~)` is met there)
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
- Not fixed: 22.04, where only the full Writer carries the file (its CI jobs do not block). The Word
  floor passes with little margin (95.08%, 6 files under 90%).
