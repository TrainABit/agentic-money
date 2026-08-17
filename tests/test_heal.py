import json
import os

from sovereign.agents.roles import improver, mechanic
from sovereign.config import EngineConfig
from sovereign.engine.heartbeat import step
from sovereign.engine.world import bootstrap
from sovereign.heal.repair import setup, thaw_cooled
from sovereign.memory.playbooks import promote_trial, revert_trial


def test_setup_repairs_corrupt_inbox_playbook_and_stale_lock(tmp_path):
    cfg = EngineConfig(mode="sim", data_dir=tmp_path)  # type: ignore[arg-type]
    world = bootstrap(cfg)
    paths = cfg.paths()
    paths.human.write_text("{not-json")
    (paths.playbooks / "mechanic.md").unlink()
    paths.lock.write_text("99999999")
    report = setup(world, full=True)
    assert json.loads(paths.human.read_text()) == []
    assert (paths.playbooks / "mechanic.md").exists()
    assert not paths.lock.exists()
    by_code = {f["code"]: f for f in report["findings"]}
    assert by_code["human_inbox"]["ok"]
    assert by_code["playbooks"]["ok"]
    assert by_code["lock"]["ok"]
    assert by_code["tools"]["ok"]
    assert any(r["code"] == "human_inbox" and r["ok"] for r in report["repairs"])


def test_cli_setup_and_doctor_fix(tmp_path):
    from sovereign.cli import main

    assert main(["init", "--data-dir", str(tmp_path), "--mode", "sim"]) == 0
    (tmp_path / "playbooks" / "hunter.md").unlink()
    (tmp_path / "human_inbox.json").write_text("NOPE")
    assert main(["setup", "--data-dir", str(tmp_path), "--mode", "sim"]) == 0
    assert (tmp_path / "playbooks" / "hunter.md").exists()
    assert json.loads((tmp_path / "human_inbox.json").read_text()) == []
    assert main(["doctor", "--fix", "--data-dir", str(tmp_path), "--mode", "sim"]) == 0


def test_thaw_after_cooldown(tmp_path):
    cfg = EngineConfig(mode="sim", data_dir=tmp_path)  # type: ignore[arg-type]
    world = bootstrap(cfg)
    world.reputation.slash("hunter", 60, "test")  # 70 -> 10
    world.freeze("hunter", "rep 10")
    world.freeze_since["hunter"] = world.tick - 5
    thawed = thaw_cooled(world, cooldown=5)
    assert "hunter" in thawed
    assert "hunter" not in world.frozen
    assert world.reputation.get("hunter") >= 20


def test_frozen_agent_skipped_until_mechanic_thaws(tmp_path):
    cfg = EngineConfig(mode="sim", data_dir=tmp_path)  # type: ignore[arg-type]
    world = bootstrap(cfg)
    world.reputation.scores["hunter"] = 10
    world.freeze("hunter", "test")
    world.freeze_since["hunter"] = world.tick  # cooldown not yet
    step(world)
    assert "hunter" in world.frozen
    world.freeze_since["hunter"] = world.tick - 5
    mechanic(world)
    assert "hunter" not in world.frozen


def test_ab_promote_and_revert(tmp_path):
    cfg = EngineConfig(mode="sim", data_dir=tmp_path)  # type: ignore[arg-type]
    world = bootstrap(cfg)
    pb = cfg.paths().playbooks
    (pb / "closer.trial.md").write_text("# Closer trial\n- Name their stack in line 1.\n")
    world.store.set_kv(
        "ab_closer",
        {"trial_n": 8, "control_n": 8, "trial_usd": 4000.0, "control_usd": 1000.0},
    )
    world.tick = 7
    out = improver(world)
    assert out and out[0]["promoted"]
    assert not (pb / "closer.trial.md").exists()
    assert "Name their stack" in (pb / "closer.md").read_text()

    (pb / "closer.trial.md").write_text("# worse\n")
    world.store.set_kv(
        "ab_closer",
        {"trial_n": 8, "control_n": 8, "trial_usd": 10.0, "control_usd": 1000.0},
    )
    world.tick = 14
    out = improver(world)
    assert out and out[0]["reverted"]
    assert not (pb / "closer.trial.md").exists()
    assert promote_trial(pb, "closer") is False
    assert revert_trial(pb, "closer") is False


def test_stale_lock_pid_alive_is_not_removed(tmp_path):
    cfg = EngineConfig(mode="sim", data_dir=tmp_path)  # type: ignore[arg-type]
    world = bootstrap(cfg)
    paths = cfg.paths()
    paths.lock.write_text(str(os.getpid()))
    report = setup(world, full=False)
    by_code = {f["code"]: f for f in report["findings"]}
    assert by_code["lock"]["ok"]
    assert paths.lock.exists()
