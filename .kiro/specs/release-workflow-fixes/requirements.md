# Micro spec: release workflow fixes before v0.1.0

## Intent

A dry run of `.github/workflows/release.yml` (2026-10-18, fresh ubuntu:24.04 containers, steps
extracted from the YAML) found two defects that would have shown on the first tag:

1. The release job downloaded every artifact into `artifacts/`. With one artifact in the run (only
   `dist-ubuntu-24.04` since 22.04 left the build matrix), download-artifact unpacks it straight into
   the path (action source at the pinned v8.0.2; its README: "When downloading a single artifact (by
   name or ID), it will always be extracted directly to the specified path"; actions/download-artifact#426),
   so `cp artifacts/dist-ubuntu-24.04/*.deb` failed and nothing was published.
2. GitHub renders each newline of release notes as a line break, so CHANGELOG.md's wrapped list
   items came out cut mid-sentence.

## Boundaries

- `release.yml` only: download by name into `artifacts/dist-ubuntu-24.04`; rejoin wrapped list lines of
  the notes. No other job, no package or app change.
- A release added to the build matrix needs its own download step (comment on the matrix).
- The rejoin covers continuation lines indented by two spaces that do not start a list item; today's
  CHANGELOG.md has no other kind.

## Acceptance

- WHEN the release job runs for a `v0.1.0` tag THEN `release/` SHALL hold the .deb, the offline bundle
  and a SHA256SUMS that verifies.
- WHEN the notes are extracted for 0.1.0 THEN each list item SHALL be one line (10 lines, 7 items).
- actionlint SHALL report no errors.

Evidence: steps re-run from the patched YAML in the dry run (3 assets, checksums verify, notes
byte-identical to the expected text, actionlint 1.7.12 with shellcheck: 0 errors); notes and
actionlint re-checked on the owner's tree before the commit.
