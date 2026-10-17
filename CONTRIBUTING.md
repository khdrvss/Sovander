# Contributing to Sovander

Thank you for helping. The most useful contributions, in order:

1. **A file Sovander shows wrongly.** Open an issue with the "File shown wrongly" form and attach the file
   (or a copy with the private parts removed) and a screenshot of how it should look.
2. **A translation.** See below; no programming needed.
3. **Code**, ideally for an issue that already describes the problem.

## Set up

On Ubuntu 22.04 or 24.04, install the packages listed in [README.md](README.md#build-and-test), then:

```sh
python3 -m sovander some-file.docx               # run from the checkout
python3 -m pytest -m "not engine and not gui"    # fast tests, a few seconds
xvfb-run -a python3 -m pytest -m gui             # the views, about two minutes
python3 -m pytest -m engine                      # with LibreOffice, about a minute
ruff check .
```

Develop against Ubuntu's own packages (`python3-fitz`, `python3-uno`, LibreOffice from the archive): they
are what users get. A newer PyMuPDF from pip, for example, hides problems the released package then has.

## How changes are made

- **Specification first.** Sovander is built from the specs in `.kiro/specs/` (requirements → design →
  tasks). A new feature starts with its requirements there; a fix says which requirement it restores.
- **A test for every fix**, one that fails without the fix.
- **Style:** Python 3.10 (Ubuntu 22.04), `ruff check .` clean, lines up to 110 characters, files under
  500 lines. Comments say *why*, not *what*.
- **Safety rules the code keeps:** nothing may open a network connection; a file from outside is
  untrusted input (bounded sizes, no execution); GTK objects are touched only on the main thread;
  signal handlers are connected through `sovander.views.weak_handler`.
- **Commits:** one change each, with a message that says what and why.

Pull requests run the CI (lint, unit, views, engine, package, and the offline gate inside AppArmor).

## Translations

Interface strings live in `po/`: `sovander.pot` is the template, one `<language>.po` per language.

- **New language:** copy `po/sovander.pot` to `po/<code>.po` (for example `uz.po`), translate it with a PO
  editor such as Poedit or Gtranslator, and add the code to `po/LINGUAS`.
- **New strings in the code:** mark them with `_()` (or `N_()` for constants) and add them to
  `po/sovander.pot` and every `.po`; `tests/unit/test_i18n.py` fails when the template and the code differ.
- The desktop entry and the metadata (`data/*.desktop`, `data/*.metainfo.xml`) carry their translations
  inline (`Name[ru]=`, `xml:lang="ru"`).

## Reporting security problems

Not in an issue, please: see [SECURITY.md](SECURITY.md).
