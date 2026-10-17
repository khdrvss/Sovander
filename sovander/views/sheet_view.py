"""Sheet view (Requirements 4.1-4.5, 6.2-6.5): sheet tabs, a formula bar and the cell grid.

``load()`` takes a sheets.Workbook. A sheet's Grid is parsed the first time its tab is shown, off the
main thread, while a loading note shows. The formula bar shows the selected cell's address and its
stored formula (or its value); a formula the Grid lacks can come from the engine on demand. ``find()``
and ``clear()`` make it a sovander.find.FindBackend: matches run over every sheet in tab order, then
rows, then columns.
"""

from __future__ import annotations

import bisect
import logging
import threading
import weakref
from collections.abc import Callable
from typing import Any

import gi

gi.require_version("Gtk", "4.0")
gi.require_version("Pango", "1.0")
from gi.repository import GLib, Gtk, Pango  # noqa: E402

from sovander import i18n  # noqa: E402
from sovander.i18n import _  # noqa: E402
from sovander.views import weak_handler  # noqa: E402
from sovander.views.sheet_grid import (  # noqa: E402
    FORMULA_DELAY_MS,
    SheetGrid,
    cell_address,
    column_letters,
    looks_numeric,
)

__all__ = ["SheetView", "cell_address", "column_letters", "looks_numeric"]

log = logging.getLogger(__name__)

FormulaSource = Callable[[str, int, int], "str | None"]  # (sheet name, row, col) -> stored formula


class SheetView(Gtk.Box):
    """One workbook: formula bar on top, the grid, sheet tabs below (Requirement 4)."""

    __gtype_name__ = "SovanderSheetView"

    def __init__(self):
        super().__init__(orientation=Gtk.Orientation.VERTICAL)
        self._book: Any = None
        self._grids: dict[int, Any] = {}
        self._index = -1
        self._formula_source: FormulaSource | None = None
        self._sheet_token = self._cell_token = self._formula_id = 0  # tokens drop late worker answers
        self._loading = self._busy = self._failed = False
        self._search: tuple[str, bool, list[tuple[int, int, int]]] | None = None
        self._search_at = -1
        self._tab_buttons: list[Gtk.ToggleButton] = []
        self._syncing = self._formatting = False
        self._keep: tuple | None = None  # (sheet, selected cell, top-left cell) kept over a preview's upgrade

        bar = Gtk.Box(spacing=8, margin_start=8, margin_end=8, margin_top=4, margin_bottom=4)
        self._address = Gtk.Label(xalign=0, width_chars=8)
        self._address.add_css_class("monospace")
        self._contents = Gtk.Label(xalign=0, hexpand=True, selectable=True, single_line_mode=True,
                                   ellipsize=Pango.EllipsizeMode.END)
        bar.append(self._address)
        bar.append(Gtk.Separator(orientation=Gtk.Orientation.VERTICAL))
        bar.append(self._contents)
        self._marker = Gtk.Box(spacing=4, visible=False)  # "Formatting…" while a preview is shown (4.6)
        self._marker_spinner, self._marker_label = Gtk.Spinner(), Gtk.Label()
        self._marker_label.add_css_class("dim-label")
        self._marker.append(self._marker_spinner)
        self._marker.append(self._marker_label)
        bar.append(self._marker)
        self.append(bar)
        self.append(Gtk.Separator())

        self.area = SheetGrid(self._on_selected)
        overlay = Gtk.Overlay(child=Gtk.ScrolledWindow(child=self.area, hexpand=True, vexpand=True))
        self._spinner = Gtk.Spinner()
        self._note = Gtk.Label(wrap=True, justify=Gtk.Justification.CENTER)
        self._note.add_css_class("dim-label")
        self._status = Gtk.Box(spacing=8, halign=Gtk.Align.CENTER, valign=Gtk.Align.CENTER, visible=False)
        self._status.append(self._spinner)
        self._status.append(self._note)
        overlay.add_overlay(self._status)
        self.append(overlay)

        self._tabs = Gtk.Box(spacing=2, margin_start=4, margin_end=4, margin_top=2, margin_bottom=2)
        self._tab_bar = Gtk.ScrolledWindow(child=self._tabs, vscrollbar_policy=Gtk.PolicyType.NEVER,
                                           propagate_natural_height=True, visible=False)
        self.append(self._tab_bar)
        i18n.add_listener(self._relabel)

    # Loading

    def load(self, workbook, formula: FormulaSource | None = None) -> None:
        """Show a sheets.Workbook from its first sheet.

        ``formula(sheet_name, row, col)`` is asked on a worker thread for a selected cell the Grid holds
        no formula for, e.g. ``functools.partial(sheets.formula, source)`` for a grid read without them.
        """
        old, keep = self._book, None
        if (getattr(old, "provisional", False) and not getattr(workbook, "provisional", False)
                and list(old.sheets) == list(workbook.sheets) and self._index >= 0):
            keep = (self._index, self.selected_cell, self._top_left())  # the preview stays until replaced
        self._book, self._grids, self._formula_source = workbook, {}, formula
        self._search, self._search_at, self._index, self._keep = None, -1, -1, keep
        self.set_formatting(getattr(workbook, "provisional", False) or keep is not None)
        self._sheet_token += 1
        if keep is None:
            self._build_tabs()
        if keep is not None:
            self.show_sheet(keep[0])
        elif workbook.sheets:
            self.show_sheet(0)
        else:
            self._show_grid(None)

    def set_formatting(self, on: bool) -> None:
        """Show or hide "Formatting…": values from a preview, the engine's formatted grid on its way."""
        self._formatting = bool(on)
        self._marker.set_visible(self._formatting)
        self._marker_spinner.set_spinning(self._formatting)
        self._marker_label.set_text(_("Formatting…"))

    @property
    def formatting(self) -> bool:
        return self._formatting

    def _top_left(self) -> tuple[int, int] | None:
        area, grid = self.area, self.area.grid
        if grid is None or area.sel is None:
            return None
        oy = area.props.vadjustment.get_value() if area.props.vadjustment else 0
        ox = area.props.hadjustment.get_value() if area.props.hadjustment else 0
        i, j = bisect.bisect_right(area._ys, oy) - 1, bisect.bisect_right(area._xs, ox) - 1
        return grid.rows[max(0, min(i, len(grid.rows) - 1))], grid.cols[max(0, min(j, len(grid.cols) - 1))]

    def _restore(self, keep) -> None:
        """Put the selection and the top-left cell back after a preview was replaced (by real coordinates)."""
        _sheet, selected, top_left = keep
        grid, area = self.area.grid, self.area
        if grid is None or not grid.rows or not grid.cols:
            return
        if top_left is not None:
            i = min(bisect.bisect_left(grid.rows, top_left[0]), len(grid.rows) - 1)
            j = min(bisect.bisect_left(grid.cols, top_left[1]), len(grid.cols) - 1)
            for adj, value in ((area.props.vadjustment, area._ys[i]), (area.props.hadjustment, area._xs[j])):
                if adj is not None:
                    adj.set_value(value)
        if selected is not None:
            self.select_cell(*selected)

    def set_loading(self, on: bool) -> None:
        """Show or hide the loading note while the engine prepares the workbook."""
        self._loading = bool(on)
        self._update_status()

    @property
    def loading(self) -> bool:
        return self._loading or self._busy

    @property
    def sheet_index(self) -> int:
        return self._index

    @property
    def sheet_names(self) -> list[str]:
        return list(self._book.sheets) if self._book is not None else []

    def show_sheet(self, index: int) -> None:
        """Switch to sheet ``index`` (0-based, tab order); a new sheet is parsed off the main thread."""
        if self._book is None or not 0 <= index < len(self._book.sheets):
            raise IndexError(index)
        self._index = index
        self._sheet_token += 1
        self._sync_tabs()
        if index in self._grids:
            self._show_grid(self._grids[index])
            return
        if self._keep is None or self._keep[0] != index:  # else the preview stays shown meanwhile
            if self._keep is not None:
                self._keep = None
                self.set_formatting(False)
            self.area.set_grid(None)
            self._busy, self._failed = True, False
            self._update_status()
            self._on_selected()
        token, book, ref = self._sheet_token, self._book, weakref.ref(self)

        def work():
            try:
                grid, error = book.grid(index), None
            except Exception as exc:  # noqa: BLE001 - shown as this sheet's error, the others still work
                grid, error = None, exc
            GLib.idle_add(_deliver, ref, "_grid_ready", token, book, index, grid, error)

        threading.Thread(target=work, name="sovander-sheet-grid", daemon=True).start()

    def _grid_ready(self, token, book, index, grid, error) -> None:
        if book is not self._book:
            return
        if grid is not None:
            self._grids[index] = grid
        if token != self._sheet_token:
            return
        keep, self._keep = self._keep, None
        if keep is not None:
            self.set_formatting(False)
        if error is not None:
            log.warning("sheet %d cannot be shown: %s", index, error)
            if keep is not None:  # the engine's grid failed: the preview stays
                return
            self._busy, self._failed = False, True
            self._update_status()
            return
        self._show_grid(grid)
        if keep is not None:
            self._restore(keep)

    def _grid_now(self, index: int):
        """A sheet's Grid, parsed here if needed (search must see every sheet); None if it fails."""
        if index not in self._grids:
            try:
                self._grids[index] = self._book.grid(index)
            except Exception:  # noqa: BLE001 - a broken sheet has no matches; the others still count
                log.warning("sheet %d cannot be searched", index, exc_info=True)
                return None
        return self._grids[index]

    def _show_grid(self, grid) -> None:
        self._busy = self._failed = False
        self.area.set_grid(grid)
        self._apply_hits()
        self._update_status()
        self._on_selected()

    def _update_status(self) -> None:
        busy, grid = self.loading, self.area.grid
        if busy:
            text = _("Loading spreadsheet…")
        elif self._failed:
            text = _("This sheet cannot be shown")
        elif self._book is not None and (grid is None or not grid.rows or not grid.cols):
            text = _("This sheet is empty")
        else:
            text = None
        self._spinner.set_visible(busy)
        self._spinner.set_spinning(busy)
        self._note.set_text(text or "")
        self._status.set_visible(text is not None)

    def _relabel(self, _language=None) -> None:
        self._update_status()
        self._marker_label.set_text(_("Formatting…"))

    # Tabs (4.1)

    def _build_tabs(self) -> None:
        for button in self._tab_buttons:
            self._tabs.remove(button)
        self._tab_buttons = []
        for index, name in enumerate(self.sheet_names):
            button = Gtk.ToggleButton(label=name)
            button.add_css_class("flat")
            if self._tab_buttons:
                button.set_group(self._tab_buttons[0])
            button.connect("toggled", weak_handler(self._on_tab), index)
            self._tabs.append(button)
            self._tab_buttons.append(button)
        self._tab_bar.set_visible(bool(self._tab_buttons))

    def _sync_tabs(self) -> None:
        self._syncing = True
        try:
            self._tab_buttons[self._index].set_active(True)
        finally:
            self._syncing = False

    def _on_tab(self, button, index) -> None:
        if button.get_active() and not self._syncing and index != self._index:
            self.show_sheet(index)

    # Selection and the formula bar (4.5)

    @property
    def selected_cell(self) -> tuple[int, int] | None:
        """The selected cell in real sheet coordinates (0-based row, col), or None."""
        grid, sel = self.area.grid, self.area.sel
        return None if grid is None or sel is None else (grid.rows[sel[0]], grid.cols[sel[1]])

    def select_cell(self, row: int, col: int) -> bool:
        """Select a cell by real sheet coordinates; False if it is hidden or outside the sheet."""
        grid = self.area.grid
        if grid is None or self.area.sel is None:
            return False
        i, j = bisect.bisect_left(grid.rows, row), bisect.bisect_left(grid.cols, col)
        if i >= len(grid.rows) or j >= len(grid.cols) or grid.rows[i] != row or grid.cols[j] != col:
            return False
        self.area.select(i, j)
        return True

    @property
    def address(self) -> str:
        return self._address.get_text()

    @property
    def formula_text(self) -> str:
        """What the formula bar shows: the stored formula, else the cell's text."""
        return self._contents.get_text()

    def _on_selected(self) -> None:
        self._cell_token += 1
        if self._formula_id:
            GLib.source_remove(self._formula_id)
            self._formula_id = 0
        cell, grid = self.selected_cell, self.area.grid
        if cell is None:
            self._address.set_text("")
            self._contents.set_text("")
            return
        shown, stored = grid.cells.get(cell), grid.formulas.get(cell)
        self._address.set_text(cell_address(*cell))
        self._contents.set_text(stored if stored is not None else (shown.text if shown is not None else ""))
        if stored is None and self._formula_source is not None:
            name = self._book.sheets[self._index]
            self._formula_id = GLib.timeout_add(FORMULA_DELAY_MS, weak_handler(self._fetch_formula),
                                                self._cell_token, name, *cell)

    def _fetch_formula(self, token, name, row, col) -> bool:
        self._formula_id = 0
        source, ref = self._formula_source, weakref.ref(self)

        def work():
            try:
                text = source(name, row, col)
            except Exception:  # noqa: BLE001 - the value stays shown; the formula is a nicety
                log.warning("formula of %s!%s not read", name, cell_address(row, col), exc_info=True)
                text = None
            GLib.idle_add(_deliver, ref, "_formula_ready", token, text)

        threading.Thread(target=work, name="sovander-sheet-formula", daemon=True).start()
        return GLib.SOURCE_REMOVE

    def _formula_ready(self, token, text) -> None:
        if token == self._cell_token and text:
            self._contents.set_text(text)

    # Find (6.2-6.5)

    def find(self, query: str, forward: bool = True, case: bool = False) -> tuple[int, int]:
        """Select the next (or previous) cell whose text contains ``query``: (0-based match, total).

        A new query starts at the selected cell itself; repeating it moves on, into the next sheet when
        this one has no more, and round to the start. Every match on the shown sheet is highlighted.
        The match is -1 when nothing matches.
        """
        if not query or self._book is None:
            self.clear()
            return -1, 0
        fresh = self._search is None or self._search[:2] != (query, case)
        if fresh:
            self._search = (query, case, self._matches(query, case))
        matches = self._search[2]
        self._apply_hits()
        if not matches:
            self._search_at = -1
            return -1, 0
        sel = self.area.sel
        here = (self._index, *sel) if sel is not None else (self._index, -1, -1)
        moving = not fresh and 0 <= self._search_at < len(matches) and matches[self._search_at] == here
        if forward:
            k = (bisect.bisect_right if moving else bisect.bisect_left)(matches, here) % len(matches)
        else:
            k = (bisect.bisect_left if moving else bisect.bisect_right)(matches, here) - 1
            k %= len(matches)
        self._search_at = k
        sheet, i, j = matches[k]
        if sheet != self._index:
            self._index = sheet
            self._sheet_token += 1
            self._sync_tabs()
            self._show_grid(self._grids[sheet])
        self.area.select(i, j)
        self._apply_hits()
        return k, len(matches)

    def clear(self) -> None:
        """Forget the search and its highlights."""
        self._search, self._search_at = None, -1
        self._apply_hits()

    def _matches(self, query: str, case: bool) -> list[tuple[int, int, int]]:
        needle = query if case else query.lower()  # lower(), not casefold(): as sovander.find matches
        found = []
        for sheet in range(len(self._book.sheets)):
            grid = self._grid_now(sheet)
            if grid is None:
                continue
            rows, cols = grid.rows, grid.cols
            for (row, col), cell in grid.cells.items():
                text = cell.text
                if text and needle in (text if case else text.lower()):
                    i, j = bisect.bisect_left(rows, row), bisect.bisect_left(cols, col)
                    if i < len(rows) and j < len(cols) and rows[i] == row and cols[j] == col:
                        found.append((sheet, i, j))
        found.sort()
        return found

    def _apply_hits(self) -> None:
        here, search, at = self._index, self._search, self._search_at
        self.area.hits = {(i, j) for s, i, j in search[2] if s == here} if search else set()
        current = search[2][at] if search and 0 <= at < len(search[2]) else None
        self.area.current_hit = current[1:] if current and current[0] == here else None
        self.area.queue_draw()


def _deliver(ref: weakref.ref, method: str, *args) -> bool:
    """Hand a worker thread's result to a view that may have closed meanwhile (GLib idle callback)."""
    view = ref()
    if view is not None:
        getattr(view, method)(*args)
    return GLib.SOURCE_REMOVE
