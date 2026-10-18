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
