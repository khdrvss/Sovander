import os
import signal
import time

import pytest

from sovander.engine.client import props
from sovander.engine.process import (
    PROFILE_SEED,
    Engine,
    EngineCrashed,
    EngineTimeout,
    EngineUnavailable,
    connection_string,
    seed_profile,
)

pytestmark = pytest.mark.engine

PROVIDERS = ("/org.openoffice.ucb.Configuration/ContentProviders/Local/SecondaryKeys/Office/ProviderData")


def config_value(eng, node_path, name):
    provider = eng.create("com.sun.star.configuration.ConfigurationProvider")
    node = provider.createInstanceWithArguments("com.sun.star.configuration.ConfigurationAccess",
                                                props(nodepath=node_path))
    return node.getByName(name)


def test_engine_runs_and_names_its_version(engine):
    assert engine.alive
    assert engine.version.startswith("LibreOffice ")


def test_private_profile_settings_are_live(engine):
    def read(eng):
        return {
            "macros off": config_value(eng, "/org.openoffice.Office.Common/Security/Scripting",
                                       "DisableMacrosExecution"),
            "calc never recalcs xlsx": config_value(eng, "/org.openoffice.Office.Calc/Formula/Load",
                                                    "OOXMLRecalcMode"),
            "writer never updates links": config_value(eng, "/org.openoffice.Office.Writer/Content/Update",
                                                       "Link"),
            "http provider": config_value(eng, PROVIDERS + "/Provider3", "ServiceName"),
            "https provider": config_value(eng, PROVIDERS + "/Provider12", "ServiceName"),
        }

    assert engine.run(read) == {"macros off": True, "calc never recalcs xlsx": 1,
                                "writer never updates links": 0, "http provider": "",
                                "https provider": ""}


def test_hung_job_is_killed_and_the_engine_restarts(engine):
    old_pid, old_generation = engine.pid, engine.generation

    def hang(eng):  # blocks inside the engine until data arrives, which never happens
        eng.create("com.sun.star.io.Pipe").readBytes(None, 1)

    started = time.monotonic()
    with pytest.raises(EngineTimeout):
        engine.run(hang, timeout=3)
    assert time.monotonic() - started < 20
    with pytest.raises(ProcessLookupError):
        os.kill(old_pid, 0)
    assert engine.run(lambda eng: eng.desktop is not None)  # waits for the restart
    assert engine.generation == old_generation + 1


def test_crash_during_a_job_restarts_the_engine(engine):
    old_generation = engine.generation

    def crash(eng):
        os.killpg(eng.pid, signal.SIGKILL)
        time.sleep(0.5)
        eng.desktop.getComponents().createEnumeration()

    with pytest.raises(EngineCrashed):
        engine.run(crash)
    assert engine.run(lambda eng: eng.desktop is not None)
    assert engine.generation == old_generation + 1


def test_job_errors_pass_through_without_restart(engine):
    generation = engine.generation
    with pytest.raises(ZeroDivisionError):
        engine.run(lambda eng: 1 / 0)
    assert engine.alive and engine.generation == generation


def test_a_failed_job_leaves_no_proxies_behind(engine):
    """repr() of a proxy whose engine died aborts the process: a test report showing a failed job's
    arguments did that on 22.04 (exit 134). Only names are compared here, never the proxies."""

    def crash(eng):
        desktop = eng.desktop
        os.killpg(eng.pid, signal.SIGKILL)
        time.sleep(0.5)
        desktop.getComponents().createEnumeration()

    with pytest.raises(EngineCrashed) as crashed:
        engine.run(crash)
    tb = crashed.value.__cause__.__traceback__
    while tb:
        assert list(tb.tb_frame.f_locals) == [], tb.tb_frame.f_code.co_name
        tb = tb.tb_next


def test_production_connection_is_a_pipe(monkeypatch):
    monkeypatch.delenv("SOVANDER_ENGINE_CONNECT", raising=False)
    assert connection_string() == f"pipe,name=sovander-{os.getuid()}"


def test_snap_engine_refuses_the_unreachable_pipe(monkeypatch):
    monkeypatch.setattr("sovander.engine.process.shutil.which", lambda name: name)  # snap or not
    with pytest.raises(EngineUnavailable, match="SOVANDER_ENGINE_CONNECT"):
        Engine(soffice="/snap/bin/libreoffice", connect="pipe,name=x").start()


def test_missing_engine_is_reported(tmp_path):
    with pytest.raises(EngineUnavailable, match="not installed"):
        Engine(soffice=str(tmp_path / "soffice"), connect="pipe,name=x").start()


def test_seed_merges_into_an_existing_profile(tmp_path):
    user = tmp_path / "user"
    user.mkdir()
    (user / "registrymodifications.xcu").write_text(
        '<?xml version="1.0" encoding="UTF-8"?>\n<oor:items xmlns:oor="http://openoffice.org/2001/registry">\n'
        '<item oor:path="/org.openoffice.Office.Common/Misc"><prop oor:name="CrashReport" oor:op="fuse">'
        "<value>true</value></prop></item>\n"
        '<item oor:path="/org.openoffice.Office.Common/Misc"><prop oor:name="Other" oor:op="fuse">'
        "<value>kept</value></prop></item>\n</oor:items>\n")
    seed_profile(tmp_path)
    text = (user / "registrymodifications.xcu").read_text()
    assert "<value>kept</value>" in text
    assert '"CrashReport" oor:op="fuse"><value>false</value>' in text
    assert '"CrashReport" oor:op="fuse"><value>true</value>' not in text
    seed_profile(tmp_path)  # idempotent
    assert (user / "registrymodifications.xcu").read_text() == text
    assert text.count("<item ") == PROFILE_SEED.read_text().count("<item ") + 1
