# Requirements: Public release on GitHub

Quick Spec (2026-10-18). Owner's request: "make it ready to go full public — GitHub, app icon, README".
Scope: everything a stranger needs to find, trust, install, report on and contribute to Sovander 0.1.0.
Publishing (creating the public repository and pushing) happens only after the owner's explicit OK.

## Requirement 1: Identity that cannot collide

**User story:** As the owner, I want Sovander's identifiers to point at my own account, so that no one
else's project can claim them and they never have to change after release.

1. The application ID shall be derived from a namespace the owner controls (the owner's GitHub account),
   so that the ID's code-hosting URL is the project's own repository.
2. The desktop entry's file name shall equal the application ID, so that the desktop shell matches the
   running window to its menu entry and icon.
3. The application ID shall be the same in the program, the desktop entry, the metadata, the icon names,
   the package and the security profile.

## Requirement 2: An app icon

**User story:** As a user, I want a clear, recognisable icon, so that I can find Sovander in the menu,
the dock and "Open With".

1. The Viewer shall ship a full-colour scalable icon that stays recognisable from 16 to 512 pixels.
2. The Viewer shall ship a single-colour (symbolic) icon for places that draw monochrome icons.
3. The icon shall follow the desktop's app icon style (a simple object, soft depth, the desktop's palette)
   and express the product: viewing documents (the name comes from "сова", owl).
4. The same icon shall appear in the menu, the dock, the About dialog, the metadata and the README.

## Requirement 3: A README that answers a stranger's questions

1. The README shall say in its first lines what Sovander is, for whom, and what makes it different
   (view-only, offline by design, accurate).
2. The README shall show real screenshots of the application.
3. The README shall list the supported formats, and say plainly what is not supported.
4. The README shall explain installation from a release package, for an offline machine, and from source.
5. The README shall explain the offline guarantee honestly: how it is enforced and what is not yet verified.
6. The README shall describe the interface languages, how to build and test, the licence, and credits.
7. A Russian translation of the README shall be provided.

## Requirement 4: Community and safety files

1. The repository shall contain the full text of its licence.
2. The repository shall explain how to report a security problem privately.
3. The repository shall explain how to contribute (setup, tests, style, translations).
4. The repository shall offer issue forms for a bug, a file shown wrongly, and a feature request, and a
   pull request template.
5. The repository shall keep a change log, starting with 0.1.0.

## Requirement 5: Releases anyone can install

1. When a version tag is pushed, the project shall build the package, run the tests, and publish a release
   with the package, the offline install bundle and their checksums.
2. The release shall only be published if the tests pass.

## Requirement 6: Metadata for software centres

1. The metadata shall give the homepage, the issue tracker, the source repository and screenshots.
2. The metadata shall describe release 0.1.0.
3. The metadata shall pass the standard validator with no errors or warnings.

## Requirement 7: Nothing private or generated goes public

1. The published repository shall contain no secrets, no personal paths or files from the developer's
   machine, no downloaded third-party corpus and no generated output.
2. Before publishing, the repository's contents shall be checked against these rules.

## Out of scope

Flathub submission, a project website, a Snap/AppImage, signing the package (a later release).
