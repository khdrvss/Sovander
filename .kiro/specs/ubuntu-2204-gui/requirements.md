# Micro spec: Ubuntu 22.04 GUI suite runs to the end

## Intent

CI's `gui (ubuntu-22.04)` job (continue-on-error) died with a segfault in every run (exit 139), after
test_home or test_sheet_view, so nobody knew what else failed on 22.04 (GTK 4.6.9, PyGObject 3.42.1,
GtkSourceView 5.4.1, shared-mime-info 2.1). Reproduced in an ubuntu:22.04 container with CI's packages.

- The segfault is in the tests only: two helpers (`drawn()` in test_home, `frame()` in test_sheet_view)
  call `Gtk.Snapshot.to_node()` and hand the node back to GTK. PyGObject 3.42 wraps GskRenderNode wrongly
  (`TypeError: expected GObject but got Gsk.ClipNode`, pygobject#471), and the next `gc.collect()` walks
  freed memory. The app never brings a render node into Python; its home screen ran 5 rounds with
  recent files and thumbnails without a warning.
- Behind it, two app bugs on 22.04:
  - text files did not open: `GtkSource.get_major_version` is in the GIR only since 5.7.1, so
    `TextView()` raised AttributeError;
  - HEIC did not open even with the loader installed: shared-mime-info 2.1 knows HEIF by file name
    only, so the bytes got no type and no loader.

## Boundaries

- `text_view._line_numbers_are_safe`: no version functions → no line numbers (5.4 leaks them too).
- `image_view._loader_by_magic`: HEIF brands find the loader that claims image/heif; used only after the
  normal load finds no loader, which does not happen on 24.04.
- Skips gated on the library, with the reason: the two render-node helpers (PyGObject < 3.44, as the
  existing `pixels` fixture), the drop test (`Gdk.FileList.new_from_list` needs GTK 4.8).
- Not done: HEIC thumbnails on 22.04 (thumbs.py uses a sniffing loader: generic icon there).

## Acceptance

- WHEN `xvfb-run -a python3 -m pytest -m gui -rs` runs on 22.04 with CI's packages THEN it SHALL finish:
  307 passed, 28 skipped, exit 0 (was a segfault).
- WHEN a text file or a HEIC photo is opened on 22.04 THEN it SHALL show (new tests
  `test_no_line_numbers_where_the_library_cannot_tell_its_version`, `test_heic_finds_its_loader_by_brand`).
- WHEN the GUI suite runs on 24.04 THEN it SHALL pass as before: 334 passed (332 + the 2 new tests).

## Extension 2026-10-18: results starved on the CI runner

With the segfault gone, CI's 22.04 job still failed 15 tests in test_app, all `still loading`, while
the same suite passed in a 22.04 container. Evidence from the hang diagnostic (commit 6cc769c): the
main context was free and always had something pending, and only the test thread was running.

- Root cause: worker threads handed their results back with `GLib.idle_add` (priority 200). The
  loading page's spinner keeps a redraw-priority source (120) due on every pass on a slow runner
  (software rendering, GTK 4.6), and GLib dispatches only the highest ready priority, so the result
  waited until the spinner went away. Reproduced locally with `docker run --cpus=0.5`: 4 of 6 tests
  failed with `still loading`.
- Fix: `views.to_main_loop` posts at `PRIORITY_HIGH_IDLE` (100), above redraw; all six worker
  handoffs use it (open job, archive entry, sheet grid and formula, find batches).
- Found behind it: an archive entry read after its window closed raised AttributeError in
  `window._open_entry` (no application left). The window now ignores it.

### Acceptance (extension)

- WHEN a worker posts a result while a redraw-priority source is always due THEN it SHALL reach the main
  loop (`tests/unit/test_main_loop.py`; fails with plain `idle_add`, checked by mutation).
- WHEN an archive entry finishes reading after its window closed THEN nothing SHALL open and no error
  SHALL be raised (`test_an_entry_read_after_its_window_closed_opens_nothing`; fails without the guard).
- WHEN test_app, test_archive_view, test_sheet_view and test_find run in a 22.04 container limited to
  half a CPU THEN they SHALL pass: 120 passed, 9 skipped (were 4 of 6 failed in test_app alone).
- WHEN the suites run on 24.04 THEN they SHALL pass as before: gui 334, unit and fast 712, engine 71.
