from datetime import timedelta
from pathlib import Path

from sovereign.comms.bus import Bus
from sovereign.config import EngineConfig
from sovereign.engine.heartbeat import step
from sovereign.engine.world import bootstrap
from sovereign.runtime.router import ClaudeCodeProvider, Router, sandbox_argv


def _provider(sandbox: str) -> ClaudeCodeProvider:
    return ClaudeCodeProvider("claude", sandbox=sandbox)


def test_sandbox_argv_binds_only_the_job_directory(tmp_path):
    home = tmp_path / "home"
    (home / ".claude").mkdir(parents=True)
    workdir = tmp_path / "work" / "job_sandbox1"
    workdir.mkdir(parents=True)
    argv = sandbox_argv(["claude", "-p", "x"], workdir, home=home)
    assert argv[0] == "bwrap"
    assert ["--ro-bind", "/", "/"] == argv[argv.index("--ro-bind") : argv.index("--ro-bind") + 3]
    assert ["--bind", str(workdir), str(workdir)] == (
        argv[argv.index(str(workdir)) - 1 : argv.index(str(workdir)) + 2]
    )
    assert str(home / ".claude") in argv
    assert "--tmpfs" in argv and "/tmp" in argv
    assert "--unshare-net" not in argv  # the CLI still needs its provider
    assert argv[argv.index("--") + 1 :] == ["claude", "-p", "x"]


def test_sandbox_modes(monkeypatch, tmp_path):
    argv = ["claude", "-p", "x"]
    workdir = tmp_path / "job_dir"
    workdir.mkdir()

    monkeypatch.setattr("sovereign.runtime.router.shutil.which", lambda name: "/usr/bin/" + name)
    assert _provider("off")._sandboxed(argv, workdir) == argv
    wrapped = _provider("auto")._sandboxed(argv, workdir)
    assert wrapped[0] == "bwrap" and wrapped[-3:] == ["--", *argv][-3:]
    assert _provider("bwrap")._sandboxed(argv, workdir)[0] == "bwrap"

    monkeypatch.setattr("sovereign.runtime.router.shutil.which", lambda name: None)
    assert _provider("auto")._sandboxed(argv, workdir) == argv
    try:
        _provider("bwrap")._sandboxed(argv, workdir)
        raise AssertionError("bwrap mode must fail closed without bubblewrap")
    except RuntimeError as exc:
        assert "bubblewrap" in str(exc)


def test_live_craft_queues_when_required_sandbox_is_missing(monkeypatch, tmp_path):
    config = EngineConfig(mode="live", data_dir=tmp_path, fetch_market_data=False)
    config.models.sandbox = "bwrap"
    router = Router(config)
    monkeypatch.setattr(router.claude, "available", lambda: True)
    monkeypatch.setattr("sovereign.runtime.router.shutil.which", lambda name: None)
    work_root = tmp_path / "work"
    workdir = work_root / "job_x"
    workdir.mkdir(parents=True)
    assert router.complete_in_dir("brief", cwd=workdir, work_root=work_root) == ""
    assert router.degraded
    assert "bubblewrap" in (router.last_error or "")


def test_router_passes_sandbox_config(tmp_path):
    config = EngineConfig(mode="sim", data_dir=tmp_path)
    config.models.sandbox = "off"
    assert Router(config).claude.sandbox == "off"


def test_heartbeat_prunes_old_done_messages(tmp_path):
    config = EngineConfig(mode="sim", data_dir=tmp_path)
    world = bootstrap(config)
    bus: Bus = world.comms
    receipt = bus.send(
        "mechanic",
        "courier",
        "notify",
        {"event": "old"},
        now=world.now - timedelta(days=30),
    )
    bus.ack(receipt.message_ids[0], now=world.now - timedelta(days=30))
    assert world.store.get_message(receipt.message_ids[0]) is not None

    # Retention runs on the tick-modulo cadence in sim; drive past one window.
    world.tick = 49
    step(world)
    assert world.store.get_message(receipt.message_ids[0]) is None
    fresh = [m for m in world.store.messages(limit=None) if m["status"] == "done"]
    assert fresh, "recent done messages must survive the prune"


def test_sim_default_keeps_sandbox_auto(tmp_path):
    config = EngineConfig(mode="sim", data_dir=Path(tmp_path))
    assert config.models.sandbox == "auto"
