"""Document views and what they share."""

import gc
import weakref

GC_INTERVAL_S = 5


def collect_garbage_on_main_thread(interval_s: int = GC_INTERVAL_S) -> int:
    """Run Python's cycle collector only on the GTK main loop; returns the GLib source id.

    The collector otherwise runs in whichever thread happens to allocate, render and thumbnail
    workers included, and a view it frees there is finalised off the main thread, which GTK aborts on.
    Reference counting still frees most objects at once; only reference cycles wait for this timer.
    """
    from gi.repository import GLib

    gc.disable()

    def collect():
        gc.collect()
        return GLib.SOURCE_CONTINUE

    return GLib.timeout_add_seconds(interval_s, collect)


def weak_handler(method):
    """A signal handler that calls ``method`` while its object lives, and lets the object go.

    PyGObject keeps a handler connected from C alive, and a bound method keeps its view alive with it,
    so a view connected with plain bound methods is never freed: every closed document would stay in
    memory. Connect through this instead.
    """
    ref = weakref.WeakMethod(method)

    def handler(*args):
        target = ref()
        return target(*args) if target is not None else False

    return handler
