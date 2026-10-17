# Requirements Document

## Introduction

A free, open-source, offline, read-only document viewer for the Ubuntu desktop. It opens the files people receive every
day (Word, Excel, PowerPoint, PDF, OpenDocument, books, images, text and zip archives) in one small app
that never touches the network, never edits the file and needs no account. It is for people who must
read Office files on Ubuntu without installing a full office suite or uploading documents to a cloud
service. The Android app Gander (MIT licence) is the model: its approach and parts of its viewer code
may be reused; its name and branding are not.

Product name: **Sovander** (from Russian "sova", owl, plus a nod to Gander). Called "the Viewer" in the criteria below.

## Requirements

### Requirement 1: Opening files

**Objective:** As an Ubuntu user, I want to open a file from wherever I find it, so that I can read it without extra steps.

#### Acceptance Criteria

1. When the user opens a supported file from the file manager with "Open With" or a double click on an associated type, the Viewer shall display that file.
2. When the user drops one or more files onto a Viewer window, the Viewer shall open each dropped file.
3. When the Viewer is started from the command line with one or more file paths, the Viewer shall open each named file.
4. When the user chooses Open in the Viewer, the Viewer shall show the system file chooser filtered to supported formats.
5. When the Viewer is installed, the Viewer shall register as an available handler for every format in Requirement 2 without changing the user's existing default applications.
6. If a file does not exist or cannot be read, the Viewer shall show a message naming the file and the reason, and shall stay running.

### Requirement 2: Supported formats

**Objective:** As a user who receives mixed files, I want one app to show all common office formats, so that I don't need a different program for each.

#### Acceptance Criteria

1. When the user opens a PDF, the Viewer shall render its pages including text, vector drawings and images.
2. When the user opens a Word file (.docx, .docm, .dotx, .doc), the Viewer shall render its text, formatting, headings, lists, tables, images, headers, footers and footnotes.
3. When the user opens an OpenDocument text (.odt) or Rich Text (.rtf) file, the Viewer shall render it with the same elements as criterion 2.2.
4. When the user opens a spreadsheet (.xlsx, .xlsm, .xlsb, .xls, .xltx, .ods, .csv), the Viewer shall render every sheet as specified in Requirement 4.
5. When the user opens a presentation (.pptx, .ppsx, .pptm, .potx, .odp, .ppt), the Viewer shall render its slides as specified in Requirement 5.
6. When the user opens an EPUB book without DRM, the Viewer shall render it chapter by chapter with its table of contents.
7. When the user opens an image (JPG, PNG, WebP, GIF, BMP, SVG, TIFF, HEIC/HEIF, AVIF), the Viewer shall display it with zoom and, for animated formats, animation.
8. When the user opens a Markdown file, the Viewer shall display it rendered as formatted text.
9. When the user opens a plain text, log or source code file, the Viewer shall display it as text in its detected encoding.
10. When the user opens an OpenDocument drawing (.odg), the Viewer shall render each of its pages.
11. The Viewer shall choose the renderer from the file's content when the file extension and the content disagree.

### Requirement 3: Reading paged documents

**Objective:** As a reader of PDFs and Word documents, I want familiar page controls, so that I can move through long documents easily.

#### Acceptance Criteria

1. While a paged document is open, the Viewer shall show the current page number and the total page count.
2. When the user enters a page number in Go to page, the Viewer shall scroll to that page.
3. When the user zooms with Ctrl+Plus, Ctrl+Minus, Ctrl+scroll or a touchpad pinch, the Viewer shall change the zoom level around the pointer position.
4. When the user chooses Fit width or Fit page, the Viewer shall size the page to that fit.
5. When the user selects text and copies it, the Viewer shall place the selected text on the system clipboard.
6. When a Word document records where its pages ended, the Viewer shall break pages at those points.
7. If a document uses a font that is not installed, the Viewer shall substitute a bundled font with the same character widths where one exists.
8. When the user opens a password-protected PDF, the Viewer shall ask for the password.
9. If the entered password is wrong, the Viewer shall say so and let the user try again.
10. If a document is partly damaged, the Viewer shall display the readable part and show a notice that the file is damaged.
11. When the user activates a link to another place in the same document, such as a table of contents entry, the Viewer shall move to that place.

### Requirement 4: Reading spreadsheets

**Objective:** As someone who receives Excel files, I want to see the sheets as the author saved them, so that I can read the numbers correctly.

#### Acceptance Criteria

1. While a spreadsheet is open, the Viewer shall show column letters, row numbers and one tab per sheet.
2. The Viewer shall display the cell values stored in the file without recalculating formulas.
3. The Viewer shall display numbers, dates, currencies and percentages in the format the file specifies.
4. The Viewer shall display merged cells, column widths, row heights and cell fills as the file specifies.
5. When the user selects a cell, the Viewer shall show that cell's address and its stored formula, if any.
6. When the user opens a sheet with more than 100,000 rows, the Viewer shall show the first screen within 3 seconds on the reference machine.

### Requirement 5: Reading presentations

**Objective:** As someone who receives slide decks, I want to page through and present them, so that I can review or show a deck without PowerPoint.

#### Acceptance Criteria

1. While a presentation is open, the Viewer shall show the current slide number and the slide count.
2. When the user presses Right, Down, Page Down or Space, the Viewer shall show the next slide.
3. When the user presses Left, Up or Page Up, the Viewer shall show the previous slide.
4. When the user starts presentation mode, the Viewer shall show one slide at a time full screen.
5. When the user presses Escape in presentation mode, the Viewer shall leave presentation mode on the current slide.
6. If a slide element cannot be drawn, the Viewer shall draw the rest of the slide and show a box in its place naming what is missing.
7. While the remaining slides of a presentation are being prepared, the Viewer shall show the slides already prepared and indicate that more are loading.

### Requirement 6: Find in document

**Objective:** As a reader, I want to search the open file, so that I can find a word without scrolling through everything.

#### Acceptance Criteria

1. When the user presses Ctrl+F, the Viewer shall show a search field.
2. When the user types a query, the Viewer shall highlight every match across all pages, sheets, slides or chapters of the open file.
3. While matches exist, the Viewer shall show the current match number and the total count.
4. When the user presses Enter or Shift+Enter in the search field, the Viewer shall move to the next or previous match.
5. The Viewer shall ignore letter case when matching unless the user turns on case matching.
6. If a query has no matches, the Viewer shall show "No matches".

### Requirement 7: Unknown and unsupported files

**Objective:** As a user with an unusual file, I want the Viewer to show what it can, so that I'm never left with a blank screen.

#### Acceptance Criteria

1. When the user opens a file of no supported format whose first bytes read as text, the Viewer shall display it as text and show a note saying so.
2. If a file of no supported format does not read as text, the Viewer shall state that the format is not supported and offer View as text.
3. When a text file is larger than 5 MB, the Viewer shall display the first 5 MB and offer Show more to load the next 5 MB.

### Requirement 8: Zip archives

**Objective:** As a user who receives zipped files, I want to browse an archive and open its files directly, so that I don't have to extract it first.

#### Acceptance Criteria

1. When the user opens a .zip file, the Viewer shall list its folders and files like a folder.
2. When the user opens an entry, the Viewer shall display it with the viewer for its format.
3. The Viewer shall not write extracted entry content to disk.
4. When an entry is password-protected, the Viewer shall ask for the password.
5. When entry names were written in a non-Unicode encoding, the Viewer shall display them in the detected encoding.
6. When the user picks a different file name encoding, the Viewer shall redisplay the names in that encoding.

### Requirement 9: Offline and privacy guarantee

**Objective:** As a privacy-minded user, I want a viewer that provably never goes online, so that my documents never leave my computer.

#### Acceptance Criteria

1. The Viewer shall not open any network connection.
2. If a document references an external resource such as a remote image, the Viewer shall not fetch it and shall show a placeholder.
3. When the user activates a link to a web address, the Viewer shall show the full address and ask for confirmation before anything opens.
4. When the user confirms a web link, the Viewer shall hand the address to the system's default browser.
5. While the confirmation for a web link is shown, the Viewer shall offer to copy the address instead.
6. The Viewer shall not collect telemetry, crash reports or usage statistics.
7. The Viewer shall run with network access denied by the operating system's application sandbox.
8. If a release build declares or acquires network access, the release verification shall fail.
9. The Viewer shall not modify, rename or write to any file the user opens.
10. The Viewer shall store recents, positions and thumbnails only in its own data folder.

### Requirement 10: Safety against hostile files

**Objective:** As a user who opens files from strangers, I want a malicious file to be harmless, so that viewing a file can't compromise my computer.

#### Acceptance Criteria

1. The Viewer shall not execute macros or scripts contained in a document.
2. When a document contains macros, the Viewer shall show a note that they are present and were not run.
3. The Viewer shall prevent content of an opened document from reading any other file on the computer.
4. If processing a file exceeds the limits in the Non-Functional Requirements, the Viewer shall stop processing it and show a message.
5. If the renderer for one document fails, the Viewer shall show an error for that document and keep other open documents working.

### Requirement 11: Recent files and resume

**Objective:** As a returning user, I want my recent files and reading positions remembered, so that I can continue where I stopped.

#### Acceptance Criteria

1. When the Viewer starts without a file, the Viewer shall show a list of recently opened files with thumbnails.
2. When the user reopens a PDF or book, the Viewer shall return to the page or position where the user left it.
3. When the user removes an entry or clears history, the Viewer shall delete the stored entry, position and thumbnail.
4. If a recent file no longer exists, the Viewer shall mark it as missing.
5. Where history is turned off, the Viewer shall record no recents, positions or thumbnails.

### Requirement 12: Display, comfort and printing

**Objective:** As a daily user, I want the Viewer to fit my desktop, so that it is comfortable to use for long reading.

#### Acceptance Criteria

1. The Viewer shall follow the system light or dark theme.
2. Where night mode is on, the Viewer shall show PDF and Word pages with light text on a dark page and leave photographs unchanged.
3. When the user presses F11, the Viewer shall toggle full screen.
4. The Viewer shall make every action reachable from the keyboard.
5. When the user chooses Print, the Viewer shall open the system print dialog with the document as displayed.

### Requirement 13: Installation on Ubuntu

**Objective:** As an Ubuntu user, I want a simple install that works fully offline afterwards, so that I can use it on machines with no internet.

#### Acceptance Criteria

1. The Viewer shall install on Ubuntu 22.04 LTS, 24.04 LTS and later on x86-64.
2. The Viewer shall provide every feature with no download after installation.
3. The Viewer shall be installable from one downloaded archive on a machine with no internet connection.
4. When the user uninstalls the Viewer, the Viewer shall leave no program files behind.

### Requirement 14: Interface languages

**Objective:** As a Russian- or English-speaking user, I want the Viewer in my language, so that every menu and message is clear to me.

#### Acceptance Criteria

1. The Viewer shall provide its complete interface in English and Russian.
2. When the Viewer starts, the Viewer shall use the language of the system locale if it is English or Russian.
3. If the system locale is neither English nor Russian, the Viewer shall use English.
4. When the user picks a language in settings, the Viewer shall switch every menu, message and dialog to that language.
5. The Viewer shall show no untranslated string in either language.

## Non-Functional Requirements

**Reference machine:** 4-core x86-64 CPU, 8 GB RAM, SSD, Ubuntu 24.04.

- **Performance:** first page of a 20-page Word document within 2 s while the Viewer is running, and within 5 s when the Viewer is started by opening it; first page of a 500-page PDF within 1.5 s; memory below 1 GB while viewing a 100 MB file.
- **Size:** the Viewer's own installed files 20 MB or less; shared system components it depends on (document engine, fonts) are named in the design and counted separately.
- **Limits:** stop processing at 200,000 archive entries, 2 GB expanded size per archive entry, or 50,000 pages.
- **Fidelity:** keep a reference corpus of at least 100 real files per format family; at least 98% of the words in the reference render must appear in the Viewer's render, measured per release.
- **Robustness:** zero crashes and zero hangs over 10,000 malformed files per format family in a fuzz run before each release.
- **Accessibility:** UI meets WCAG 2.1 AA contrast; PDF and Word text is readable by a screen reader.
- **Text:** documents in right-to-left and CJK scripts render in their reading direction.
- **Licensing:** every bundled component permits redistribution in an open-source app, and its licence text is shown in About.

## Constraints and Assumptions

- View only. Editing is out of scope for every version planned here.
- Free and open source: the code is published under an OSI-approved licence.
- Gander code (MIT) may be reused with attribution. Its name, icon and branding will not be used.
- Ubuntu on x86-64 is the target. Other distributions are not tested in v1 but not deliberately blocked.
- Audience: anyone on Ubuntu who receives Office files and wants to read them offline.

## Out of Scope (v1)

Editing, annotations and form filling; cloud, sync, accounts and collaboration; audio and video playback (Ubuntu ships players); running macros; OCR; arm64; Windows and macOS builds.

## Clarifications (2026-10-17)

1. Model: free and open source.
2. Web links: confirm, then open in the default browser, with Copy as the alternative (criteria 9.3–9.5). Confirmed by the owner.
3. OpenDocument presentations and drawings (.odp, .odg): in v1 (criteria 2.5, 2.10).
4. Interface languages: English and Russian (Requirement 14).
5. Product name: Sovander. No software product of that name was found on 2026-10-17 (web search, not a legal trademark search).
6. arm64: not in v1.

## Extension (after the engine spike, 2026-10-17)

The spike (`spike/RESULTS.md`) compared Gander's renderers with a full layout engine on 149 real files.
The owner chose the layout engine. Changes made here: legacy .ppt is supported (2.5) and 5.7 now covers
slides that take time to prepare; 13.3 allows one archive instead of one package file; the size and
performance targets separate the Viewer's own files and a cold start (engine start-up measured at
7–10 s in an unoptimised build, so the 5 s cold target must be proven in implementation).

## Extension (retrospective 2026-10-17: found in the final verification)

Emergency fixes made while verifying the finished build, without a spec item first; written up here
after the fact. Evidence: tasks.md C8–C11.

- 4.7 When a CSV file uses ';' between fields and ',' inside numbers (Excel in Russian and most of
  Europe), the Viewer shall split it at the semicolons, in the first screen and the full sheet alike.
- 4.8 When a row is shorter than a line of cell text, the Viewer shall align the text to the bottom of
  the cell, as spreadsheet applications do, so that letters reaching below the line stay visible.
- 10.6 If a spreadsheet addresses a cell past column XFD or row 1,048,576, holds more than 32,767
  characters in a cell, or has a part that unpacks to more than 64 MB, then the Viewer shall leave
  that content out of the first screen and still open the document.
- 10.7 When the Viewer ends in any way, including a crash or the loss of its display, the Viewer shall
  leave no document engine process running.

## Glossary

- **Viewer:** the application specified here.
- **Render:** to draw a file's content on screen.
- **Reference corpus:** a fixed set of real files used to measure fidelity on every release.
- **Reference render:** how a mainstream office suite shows the same file, used as the baseline for fidelity.
