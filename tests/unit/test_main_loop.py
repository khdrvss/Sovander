"""Worker results reach the main loop while a spinner keeps redrawing (Ubuntu 22.04 CI runner)."""

import threading

import pytest

gi = pytest.importorskip("gi")
from gi.repository import GLib  # noqa: E402

from sovander.views import to_main_loop  # noqa: E402

REDRAW = GLib.PRIORITY_HIGH_IDLE + 20  # GDK_PRIORITY_REDRAW, where a spinner's frames run


def handed_over(post) -> list:
    """What a worker's ``post(callback, value)`` delivers while a redraw-priority source is always due."""
    context = GLib.MainContext.default()
    busy = GLib.idle_add(lambda: GLib.SOURCE_CONTINUE, priority=REDRAW)  # a frame always due
    got = []
    try:
        worker = threading.Thread(target=post, args=(got.append, "result"))
        worker.start()
        worker.join()
        for _ in range(200):
            if got:
                break
            context.iteration(False)
        return list(got)
    finally:
        GLib.source_remove(busy)
        while context.pending():  # let a starved callback run now, not in a later test
            context.iteration(False)


def test_a_result_reaches_the_main_loop_past_constant_redraws():
    assert handed_over(to_main_loop) == ["result"]


def test_at_idle_priority_it_would_wait_for_good():
    # The cause: GLib runs only the highest ready priority, and idle is below redraw
    assert handed_over(GLib.idle_add) == []
