# Micro spec: LibreOffice 7.3 (Ubuntu 22.04) engine compatibility, step 1

## Intent

Ubuntu 22.04 support is "in progress"; its CI jobs do not block. Two of its engine failures have causes
in LibreOffice 7.3 itself (reproduced in an ubuntu:22.04 container with CI's packages):

1. Impress crashes on every slide deck: 7.3's -nogui packages ship no `.ui` files, building the view
   tab bar without `modules/simpress/ui/tabviewbar.ui` fails, and unwinding segfaults
   (sd ViewTabBar.cxx; Debian #1028290, tdf#155411). A plain `soffice --convert-to` crashes the same way.
2. A damaged .docx is reported as damaged instead of repaired: before 24.2 LibreOffice opens OOXML zips
   with repair hard-coded off (oox zipstorage.cxx; tdf#96401, commit 86c682273d90).

## Boundaries

- Engine profile: Impress's view tab bar resource gets no factory (`MultiPaneGUI/Framework/
  ResourceFactories`, F2/R0). From 7.6 the factory map is built in and the path is unknown, so 24.04's
  LibreOffice ignores the item.
- Tests: the two repair tests skip before LibreOffice 24.2 (`repairs_ooxml`), with the reason shown.
- Not fixed here: Writer's comment crash on 22.04 (`annotation.ui` ships only in the full Writer there;
  fixed by depending on it, `.kiro/specs/writer-comment-crash/`, extension 2026-10-18), slides fidelity on
  7.3 (87.7%: its PDF splits words), and the 22.04 GUI segfault (PyGObject 3.42).

## Acceptance

- WHEN a slide deck is converted on LibreOffice 7.3 THEN the engine SHALL stay running (22.04 slides
  fidelity: 0% → 87.7%).
- WHEN the engine is LibreOffice 24.2 (Ubuntu 24.04) THEN conversions and fidelity SHALL be unchanged
  (host: engine tests and fidelity re-run after the change).
