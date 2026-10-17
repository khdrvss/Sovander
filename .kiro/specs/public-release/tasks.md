# Implementation Plan

- [x] 1. Identity
- [x] 1.1 Rename the application ID to io.github.khdrvss.Sovander and the desktop entry to `<id>.desktop`
  - program, desktop entry, metadata, icon names, package build, AppArmor profile, tests
  - _Requirements: 1.1, 1.2, 1.3_
  - Факт: 8 files, 13 mentions, 3 files renamed; desktop entry now `<id>.desktop`; 77 packaging/app/About tests green; desktop-file-validate clean
- [x] 1.2 Icon: full-colour and symbolic SVG (D3), renders at 16–512 px, used in About and metadata
  - `scripts/render-icons.py` writes `docs/icon-512.png` and the social preview
  - _Requirements: 2.1, 2.2, 2.3, 2.4_
  - Факт: page-as-owl icon (ear tufts, facial disc, eyes, beak, text lines) and symbolic (eyes, beak, lines cut out); rendered and looked at on light and dark at 16–512 px; two rounds each (white page edge, cat → owl disc; symbolic robot → beak); docs/icon.png, docs/social-preview.png by scripts/render-icons.py

- [x] 2. Documentation
- [x] 2.1 Screenshots from the real app (`scripts/screenshots.sh`, D4) into `docs/screenshots/`
  - _Requirements: 3.2, 6.1_
  - Факт: scripts/screenshots.sh (light theme, 1200×780, window only, fresh home) + scripts/make-showcase.py (report.docx, budget.xlsx, talk.pptx via UNO; almanac.epub by hand) in docs/showcase; preview set taken with the checkout (shows the unprotected banner) — final set from the installed .deb pending install
- [x] 2.2 README.md and README.ru.md
  - _Requirements: 3.1, 3.3, 3.4, 3.5, 3.6, 3.7_
  - Факт: README.md and README.ru.md; claims checked against code (PDF passwords: render.py authenticate; no --version, so About; SOVANDER_LOG); names kept in their own Latin spelling
- [x] 2.3 LICENSE, SECURITY.md, CONTRIBUTING.md, CHANGELOG.md, issue forms, PR template
  - _Requirements: 4.1, 4.2, 4.3, 4.4, 4.5_
  - Факт: LICENSE = AGPL-3.0 text, sha256 0d96a4ff…abcb0 (canonical), spot-checked against gnu.org; SECURITY.md, CONTRIBUTING.md, CHANGELOG.md, 3 issue forms + config, PR template (YAML parsed)

- [x] 3. Release and metadata
- [x] 3.1 Release workflow on tags (D5)
  - _Requirements: 5.1, 5.2_
  - Факт: release.yml (tests → build matrix 22.04/24.04 with offline bundles → release with SHA256SUMS and CHANGELOG notes, contents: write only in the last job, actions pinned as in ci.yml); tag/version check and notes extraction tried locally; actionlint runs in CI
- [x] 3.2 Metainfo: homepage, bug tracker, source, screenshots, release 0.1.0; pedantic validation clean
  - _Requirements: 6.1, 6.2, 6.3_
  - Факт: homepage, bugtracker, vcs-browser, contribute, 5 screenshots with en/ru captions, release 0.1.0; `appstreamcli validate`: successful (one pedantic note: the ID's capital, GNOME convention); About shows the page (D8)

- [ ] 4. Publish
- [x] 4.1 Pre-publish scan (secrets, personal paths, generated/third-party files), .gitignore, full test run
  - _Requirements: 7.1, 7.2_
  - Факт: no secrets; personal paths in 2 spike scripts made relative; generated spike output, logs and the corpus's extracted words ignored; private notes removed from the specs; 292 files, 3.5 MB; ruff clean, 1025 + 75 tests green, .deb builds
- [ ] 4.2 First commit on main; with the owner's OK: public repository khdrvss/Sovander, push, topics
  - _Requirements: 7.1, 5.1_

## Progress

| Wave | Tasks | Status |
|---|---|---|
| 1 | 1.1, 1.2 | done |
| 2 | 2.1–2.3 | done; final screenshots wait for the installed .deb |
| 3 | 3.1, 3.2 | done |
| 4 | 4.1, 4.2 | 4.1 done; 4.2 waits for the owner's OK |
