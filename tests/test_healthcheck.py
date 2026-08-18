"""Exec-probe healthcheck: readiness plus optional tick staleness."""

from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone

from sovereign.cli import main
from sovereign.config import EngineConfig
from sovereign.engine.world import bootstrap
from sovereign.ops import healthcheck


def test_healthcheck_ready_on_fresh_sim_without_staleness_bound(tmp_path):
    world = bootstrap(EngineConfig(mode="sim", data_dir=tmp_path))  # type: ignore[arg-type]
    report = healthcheck(world)
    assert report["ok"] is True
    assert report["ready"] is True
    assert report["stale"] is False
    assert report["mode"] == "sim"
    assert report["reasons"] == []


def test_healthcheck_stale_when_no_tick_and_bound_requested(tmp_path):
    world = bootstrap(EngineConfig(mode="sim", data_dir=tmp_path))  # type: ignore[arg-type]
    world.store.set_kv("tick_start", None)
    world.store.set_kv("meta", {})
    report = healthcheck(world, max_staleness_seconds=30)
    assert report["ok"] is False
    assert report["ready"] is True
    assert report["stale"] is True
    assert any("no tick timestamp" in reason for reason in report["reasons"])


def test_healthcheck_accepts_recent_marker_then_stales(tmp_path):
    world = bootstrap(EngineConfig(mode="sim", data_dir=tmp_path))  # type: ignore[arg-type]
    now = datetime.now(timezone.utc).isoformat()
    world.store.set_kv(
        "tick_start",
        {"tick": 0, "started_ts": now, "completed_ts": now, "status": "completed"},
    )
    fresh = healthcheck(world, max_staleness_seconds=10_000)
    assert fresh["ok"] is True
    assert fresh["stale"] is False
    assert fresh["last_tick_ts"]

    world.store.set_kv(
        "tick_start",
        {
            "tick": 0,
            "started_ts": (datetime.now(timezone.utc) - timedelta(hours=2)).isoformat(),
            "completed_ts": (datetime.now(timezone.utc) - timedelta(hours=2)).isoformat(),
            "status": "completed",
        },
    )
    stale = healthcheck(world, max_staleness_seconds=60)
    assert stale["ok"] is False
    assert stale["ready"] is True
    assert stale["stale"] is True
    assert any("old" in reason for reason in stale["reasons"])


def test_healthcheck_never_raises_on_broken_world(tmp_path):
    world = bootstrap(EngineConfig(mode="sim", data_dir=tmp_path))  # type: ignore[arg-type]
    world.store.close()
    report = healthcheck(world, max_staleness_seconds=30)
    assert report["ok"] is False
    assert report["reasons"]


def test_cli_healthcheck_exit_codes(tmp_path, capsys):
    assert main(["init", "--data-dir", str(tmp_path), "--mode", "sim"]) == 0
    capsys.readouterr()
    assert main(["healthcheck", "--data-dir", str(tmp_path), "--mode", "sim"]) == 0
    payload = json.loads(capsys.readouterr().out)
    assert payload["ok"] is True
    assert payload["ready"] is True

    world = bootstrap(EngineConfig(mode="sim", data_dir=tmp_path))  # type: ignore[arg-type]
    world.store.set_kv(
        "tick_start",
        {
            "tick": 0,
            "started_ts": (datetime.now(timezone.utc) - timedelta(hours=3)).isoformat(),
            "completed_ts": (datetime.now(timezone.utc) - timedelta(hours=3)).isoformat(),
            "status": "completed",
        },
    )
    world.store.close()
    assert (
        main(
            [
                "healthcheck",
                "--data-dir",
                str(tmp_path),
                "--mode",
                "sim",
                "--stale-seconds",
                "60",
            ]
        )
        == 1
    )
    stale = json.loads(capsys.readouterr().out)
    assert stale["ok"] is False
    assert stale["stale"] is True
