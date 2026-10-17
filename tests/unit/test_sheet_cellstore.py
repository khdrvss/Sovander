"""Compact grid storage (NFR memory): the Grid.cells mapping behaves like the dict it replaced."""

from sovander.engine.sheets import Cell, CellStore, Grid


def test_store_reads_back_what_was_written_in_order():
    cells = {(0, 0): Cell("a"), (5, 16383): Cell("b", "#FFCC00"), (1_048_575, 3): Cell("", None, 2, 3)}
    store = CellStore(cells)
    assert store == cells and dict(store.items()) == cells and list(store) == list(cells)
    assert list(store.values()) == list(cells.values()) and len(store) == 3
    assert store[(5, 16383)].fill == "#FFCC00" and store.get((1_048_575, 3)).colspan == 3
    assert store.get((9, 9)) is None and store.get((9, 9), "x") == "x"
    assert (0, 0) in store and (0, 1) not in store and "A1" not in store


def test_fills_are_shared_and_plain_cells_keep_only_text():
    store = CellStore()
    for col in range(3):
        store[(0, col)] = Cell("v", "".join(["#FF", "0000"]))
    store[(1, 0)] = Cell("plain")
    raw = store._cells
    assert len({id(cell.fill) for key, cell in raw.items() if not isinstance(cell, str)}) == 1
    assert raw[1 << 24] == "plain"


def test_grid_takes_a_plain_dict_and_compares_to_dicts():
    grid = Grid("S", [0], [0], [17], [64], {(0, 0): Cell("x")})
    assert isinstance(grid.cells, CellStore) and grid.cells == {(0, 0): Cell("x")}
    assert Grid("E", [], [], [], []).cells == {}
