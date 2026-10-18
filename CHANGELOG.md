# Changelog

All notable changes to Sovander. Versions follow [Semantic Versioning](https://semver.org/).

## Unreleased

- **Fixed:** on a slow machine a document could stay on the loading spinner for good: results of
  background work now reach the window ahead of the spinner's redraws.
- **Fixed:** closing an archive's window while one of its files was still being read no longer raises
  an error.

## 0.1.0 — first public release

The first release: a view-only, offline document viewer for Ubuntu 24.04 (22.04 support is in progress).

- **Opens** Word (DOCX, DOCM, DOTX, DOC), RTF, OpenDocument (ODT, ODS, ODP, ODG), Excel (XLSX, XLSM, XLSB,
  XLTX, XLS), CSV and TSV, PowerPoint (PPTX, PPSX, PPTM, POTX, PPT), PDF, EPUB, Markdown, text and source
  code, images, and ZIP archives.
- **Accurate layout** by LibreOffice's engine, kept running in the background, with pages drawn by MuPDF;
  page 1 first, then the rest of the document.
- **Large spreadsheets** show their first screen at once (XLSX, CSV, ODS read directly) while the engine
  prepares the formatted sheet.
- **Offline by design:** an AppArmor profile denies the network to Sovander and everything it starts; a
  start-up check shows when protection is off. Links open only after confirmation, through the desktop.
- **Safe with hostile files:** macros never run, sizes and archive contents are bounded, and the engine is
  ended with the app however the app ends.
- Find in the document, night mode, zoom, slide show, printing, recent files with previews.
- English and Russian interface.
