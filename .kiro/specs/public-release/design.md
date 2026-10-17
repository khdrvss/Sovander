# Design: Public release on GitHub

## Decisions

**D1 — Application ID `io.github.khdrvss.Sovander` (was `io.github.sovander.Sovander`).** Code-hosting IDs
map to a repository URL: Flathub computes `https://github.com/<user>/<last component>` from the ID and
requires it to be reachable, and renaming after publication means resubmitting [1]. AppStream bases ID
uniqueness on owning the domain behind it [2]. `github.com/sovander` is someone else's (empty) account,
created 2025-09-23 (checked with `gh api users/sovander`); `github.com/khdrvss` is the owner's (name
Saidxon Xaydarov). Nothing is released yet, so the rename costs nothing now and is impossible later.
Rejected: keeping the old ID (points at a stranger); a GitHub organisation (the name is taken).

**D2 — Desktop entry named `io.github.khdrvss.Sovander.desktop`** (was `sovander.desktop`). GTK sets the
window's application ID (Wayland app_id) to the GApplication ID, and GNOME Shell finds the desktop entry by
that name; with a different file name the dock shows a generic icon. AppStream's `<launchable>` names it.

**D3 — Icon: the page is the owl.** "Sov-" is сова, owl; the product is looking at documents. GNOME's
guidelines ask for one simple object, no mascots or characters, a 128×128 canvas the icon does not fill,
flat colours from the palette on flat surfaces, depth from a darker bottom profile of at most 4 px, and
legibility at 64 and 32 px; every app also needs a symbolic version [3]. So not an owl drawing: a page
whose top corners rise as two ear tufts; its upper part is blue (#3584e4) with two large amber eyes
(#f6d32d, ring #e5a50a, pupils #241f31) that also read as reading lenses, and a small orange beak
(#e66100); below a curved edge the page is white with two lines of text (#c0bfbc) and a darker bottom
profile (#deddda). Symbolic version: the same silhouette with the eyes cut out, #2e3436. Files:
`data/icons/hicolor/scalable/apps/<id>.svg`, `data/icons/hicolor/symbolic/apps/<id>-symbolic.svg`; PNG
renders for README and the social preview by `scripts/render-icons.py` (GdkPixbuf, already a
dependency). Rejected: an owl character (a mascot, against [3]); the placeholder (a page with eyes drawn
on it, outlined, unbalanced at small sizes).

**D4 — Screenshots from the real app, made by a script.** `scripts/screenshots.sh` launches the installed
or development app under Xvfb in light theme at 1200×800 with English UI, opens the repository's own
fixture files (no third-party documents in public images), and saves `docs/screenshots/*.png`: home screen
with recent files, a Word document, a spreadsheet with fills, slides, a book (EPUB). Re-runnable for every
release.

**D5 — Releases by tag.** `.github/workflows/release.yml`: on `v*` tags, on ubuntu-24.04: install the
archive dependencies, run unit + gui + packaging tests, build the .deb (`packaging/build-deb.sh`) and the
offline bundle (`packaging/make-offline-bundle.sh`), write `SHA256SUMS`, publish with `gh release create`
(token permission `contents: write`). The CI workflow stays the gate for every push.

**D6 — README in English and Russian** (`README.md`, `README.ru.md`, linked to each other). The interface
is English and Russian, and Russian-speaking users are the first audience.

**D7 — Branch `main`.** The repository has no commits yet; the first one goes on `main`.

**D8 — The project page in About.** Reverses an earlier choice (About had no website: "no address to
follow from an offline app"). A public project needs a visible way to its page and issue tracker; the
app itself stays offline, because the link goes through the same confirmation as a link in a document
(Requirement 9.3 of the viewer) and is opened by the desktop's browser, outside Sovander's profile.

## File plan

| File | Change |
|---|---|
| `sovander/__init__.py`, `data/*.metainfo.xml`, `data/*.desktop`, `packaging/build-deb.sh`, `packaging/apparmor/sovander`, tests | D1, D2 |
| `data/icons/hicolor/{scalable,symbolic}/apps/…` | D3 |
| `scripts/render-icons.py`, `scripts/screenshots.sh`, `docs/screenshots/`, `docs/icon-512.png` | D3, D4 |
| `README.md`, `README.ru.md` | R3 |
| `LICENSE` (AGPL-3.0 text from gnu.org), `SECURITY.md`, `CONTRIBUTING.md`, `CHANGELOG.md` | R4 |
| `.github/ISSUE_TEMPLATE/{bug,rendering,feature}.yml`, `.github/ISSUE_TEMPLATE/config.yml`, `.github/pull_request_template.md` | R4 |
| `.github/workflows/release.yml` | R5 |
| `data/<id>.metainfo.xml` (urls, screenshots, release) | R6 |
| `.gitignore` (spike's generated output) | R7 |

## Verification

- `appstreamcli validate --pedantic` and `desktop-file-validate`: no errors, no warnings.
- Unit + gui + packaging tests and the engine suite green; the .deb builds and installs (`apt-get install -s`).
- The icon rendered at 16, 32, 64, 128, 512 px and looked at.
- Before the first commit: a scan of every file to be committed for secrets, personal paths and generated
  or third-party files (R7).

## Sources

1. Flathub, Requirements — Application ID: https://docs.flathub.org/docs/for-app-authors/requirements
2. AppStream, Metadata — `<id/>`: https://www.freedesktop.org/software/appstream/docs/chap-Metadata.html
3. GNOME Human Interface Guidelines — App icons: https://developer.gnome.org/hig/guidelines/app-icons.html
