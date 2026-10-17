"""The engine guard with a stand-in engine (a sleep in a session of its own): no LibreOffice needed."""

import os
import subprocess
import time

from sovander.engine.process import _release_guard, _start_guard


def stand_in():
    return subprocess.Popen(["sleep", "60"], start_new_session=True)


def ended(proc, timeout=5.0) -> bool:
    try:
        proc.wait(timeout)
        return True
    except subprocess.TimeoutExpired:
        return False


def test_the_guard_kills_the_group_when_its_owner_is_gone():
    engine = stand_in()
    try:
        guard, write_end = _start_guard(engine.pid)
        os.close(write_end)  # what the kernel does when the app ends in any way
        assert ended(engine) and engine.returncode == -9
        assert ended(guard)
    finally:
        engine.kill()
        engine.wait()


def test_a_released_guard_leaves_the_group_alone():
    engine = stand_in()
    try:
        guard, write_end = _start_guard(engine.pid)
        _release_guard(guard, write_end)
        assert guard.returncode == 0
        time.sleep(0.2)
        assert engine.poll() is None  # stopping the engine is the app's job; the guard only says goodbye
    finally:
        engine.kill()
        engine.wait()
