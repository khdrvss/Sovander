# Micro spec: line numbers only where GtkSourceView frees the view

## Intent

C16 turned line numbers off below GtkSourceView 5.12.1, because 5.12.0 (Ubuntu 24.04) never frees a
view that shows them. Measured on 2026-10-18 (real TextView, 200 open/close cycles per scenario,
on this machine and in fresh ubuntu:24.04 containers with GtkSourceView built from upstream tags):

- the leak is real: with line numbers on, 0 of 200 closed views and buffers are freed, RSS grows about
  3.1 MiB per closed 256 KiB file and 10.8 MiB per 1 MB file; off, 200 of 200 are freed and RSS stays flat;
- it has two causes. The gutter renderers held the view (fixed in 5.12.1, commit 8a22882e, issue #365),
  and so did the lines the gutter last drew (fixed in 5.21.0, commit 9cb5802f, MR !438; 5.22.0 NEWS:
  "Break a circular reference when line numbers are enabled"). 5.12.1 and 5.14.2 still leak every
  drawn view; 5.12.1 with 9cb5802f frees 200 of 200;
- the repo's memory test could not see it: it held a Python weakref to the outer TextView, which is
  always freed, while the GtkSource.View and its buffer live on in C.

## Boundaries

- `_line_numbers_are_safe` gates at 5.21.0, with a docstring naming both upstream fixes. On Ubuntu 24.04
  nothing changes at runtime (still no line numbers); releases with 5.12.1 to 5.20 (Ubuntu 25.04 to
  26.04, Debian 13) no longer turn on a leaking gutter.
- `test_closed_view_is_freed_with_its_text` also watches the GtkSource.View and its buffer from C.
- Not done: a workaround that breaks both cycles by hand on older versions (measured leak-free, but it
  leans on library internals); kept as an option.

## Acceptance

- WHEN the text view runs on GtkSourceView below 5.21 THEN it SHALL show no line numbers.
- WHEN a closed text view's GtkSource.View or buffer stays alive THEN the memory test SHALL fail
  (checked: line numbers forced on, 5.12.0: `assert [] == ['buffer', 'view']`); without the leak it
  SHALL pass (23 of 23).
