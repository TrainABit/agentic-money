"""Multi-process agent waves: order, isolation, heartbeat opt-in."""

from __future__ import annotations

import json

from sovereign.cli import main
from sovereign.config import EngineConfig
from sovereign.engine.heartbeat import step
from sovereign.engine.workers import (
    PIPELINE_NAMES,
    WAVES,
    apply_worker_patches,
    run_agent_job,
    stays_in_process,
)
from sovereign.engine.world import bootstrap
from sovereign.markets.paper import PaperBroker


def test_waves_cover_roster_and_preserve_craft_order():
    flat = tuple(name for wave in WAVES for name in wave)
    assert flat == PIPELINE_NAMES
    assert len(PIPELINE_NAMES) == 16
    assert PIPELINE_NAMES.index("hunter") < PIPELINE_NAMES.index("closer")
    assert PIPELINE_NAMES.index("closer") < PIPELINE_NAMES.index("crafter")
    assert stays_in_process("mechanic", EngineConfig())
    assert stays_in_process("courier", EngineConfig())
    assert not stays_in_process("hunter", EngineConfig())


def test_run_agent_job_does_not_advance_tick(tmp_path):
    cfg = EngineConfig(mode="sim", data_dir=tmp_path)
    parent = bootstrap(cfg)
    parent.persist_kv()
    tick_before = parent.tick
    result = run_agent_job(
        {"config": cfg.model_dump(mode="json"), "agent": "bookkeeper"}
    )
    assert result["ok"] is True
    assert result["agent"] == "bookkeeper"
    parent.load_kv()
    assert parent.tick == tick_before
    events = parent.store.events(50)
    assert any(event.get("agent") == "bookkeeper" for event in events)


def test_apply_worker_patches_keeps_non_broker_snapshot():
    class _World:
        def __init__(self):
            self.broker = PaperBroker(cash=10)
            self.frozen = set()
            self.freeze_info = {}
            self.freeze_since = {}
            self.last_prices = {}
            class _Rep:
                scores = {}
            self.reputation = _Rep()
            class _Led:
                _bal_cache = {"x": 1}
            self.ledger = _Led()
            self.now = None

    world = _World()
    apply_worker_patches(
        world,
        [
            {
                "agent": "ethics",
                "broker": {"cash": 99, "position": 3, "venue": "paper"},
                "frozen": ["hunter"],
                "freeze_info": {"hunter": {"kind": "ethics"}},
                "freeze_since": {"hunter": 1},
                "reputation": {"hunter": 40},
            },
            {
                "agent": "bookkeeper",
                "broker": {
                    "cash": 50,
                    "position": 0,
                    "venue": "paper",
                    "last_price": 1,
                    "frozen": False,
                },
            },
        ],
    )
    assert world.broker.cash == 50
    assert "hunter" in world.frozen
    assert world.reputation.scores["hunter"] == 40
    assert world.ledger._bal_cache is None


def test_heartbeat_workers_all_in_process_matches_tick(tmp_path):
    sequential = EngineConfig(mode="sim", data_dir=tmp_path / "seq")
    left = bootstrap(sequential)
    r1 = step(left)

    parallel = EngineConfig(mode="sim", data_dir=tmp_path / "par")
    parallel.workers.enabled = True
    parallel.workers.in_process = PIPELINE_NAMES
    right = bootstrap(parallel)
    r2 = step(right)
    assert r1["tick"] == r2["tick"] == 1
    assert r2["errors"] == 0
    assert set(r1["pipeline"]) == set(r2["pipeline"])


def test_heartbeat_one_remote_agent_via_same_process_job(tmp_path):
    cfg = EngineConfig(mode="sim", data_dir=tmp_path)
    cfg.workers.enabled = True
    cfg.workers.max_procs = 0
    cfg.workers.in_process = tuple(name for name in PIPELINE_NAMES if name != "bookkeeper")
    world = bootstrap(cfg)
    report = step(world)
    assert report["tick"] == 1
    assert report["errors"] == 0
    assert any(event.get("agent") == "bookkeeper" for event in world.store.events(80))


def test_cli_worker_once(tmp_path, capsys):
    assert main(["init", "--data-dir", str(tmp_path), "--mode", "sim"]) == 0
    capsys.readouterr()
    code = main(
        [
            "worker",
            "--data-dir",
            str(tmp_path),
            "--mode",
            "sim",
            "--agent",
            "bookkeeper",
            "--once",
        ]
    )
    assert code == 0
    payload = json.loads(capsys.readouterr().out)
    assert payload["ok"] is True
    assert payload["agent"] == "bookkeeper"
