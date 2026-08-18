"""Debug tracing (TraceCollector, registry stats, trace-aware heartbeat) and
runtime efficiency (queued-gated inbox pump, idle throttle, shallow health
checks, `sovereign debug`). Deterministic sim mode throughout."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from sovereign.cli import main
from sovereign.config import DebugConfig, EngineConfig
from sovereign.debug import TraceCollector
from sovereign.engine import daemon as daemon_module
from sovereign.engine.daemon import serve
from sovereign.engine.heartbeat import TICK_METRICS_KEY, step
from sovereign.engine.world import bootstrap
from sovereign.heal.checks import diagnose

PIPELINE_AGENTS = (
    "mechanic", "bookkeeper", "risk", "ethics", "director", "hunter",
    "closer", "crafter", "trader", "publisher", "scout", "operator",
    "treasurer", "auditor", "improver", "courier",
)


def sim_world(tmp_path, **overrides):
    cfg = EngineConfig(mode="sim", data_dir=tmp_path, **overrides)  # type: ignore[arg-type]
    return bootstrap(cfg)


def events_of(world, kind: str) -> list[dict[str, Any]]:
    return [e for e in world.store.events(500) if e["kind"] == kind]


def stub_all_roles(monkeypatch) -> None:
    """Replace every pipeline role with a no-op so nothing creates jobs or
    queues bus messages; the heartbeat's own machinery stays fully real."""
    for name in PIPELINE_AGENTS:
        def make_stub(agent_name):
            def stub(w):
                return []
            stub.__name__ = agent_name
            return stub

        monkeypatch.setattr(f"sovereign.agents.roles.{name}", make_stub(name))


# ------------------------------------------------------------ TraceCollector


def test_trace_collector_disabled_is_noop_and_writes_nothing(tmp_path):
    trace_dir = tmp_path / "trace"
    collector = TraceCollector(trace_dir, DebugConfig(), env={})
    assert collector.enabled is False

    collector.begin_tick(1, "2026-08-18T00:00:00+00:00")
    collector.record_tool("mechanic", "heal.repair", 12.5, True)
    collector.record_agent("trader", 3.0, 1, error="x", traceback_tail="tb")
    collector.record_comms(2, 1, 0.4)
    assert collector.end_tick({"tick": 1, "duration_ms": 20.0}) is None

    assert not trace_dir.exists()  # zero filesystem writes while disabled
    assert collector.latest() == []


def test_trace_collector_env_enabled_writes_jsonl_and_enforces_retention(
    tmp_path, monkeypatch
):
    monkeypatch.setenv("SOVEREIGN_DEBUG", "1")
    trace_dir = tmp_path / "trace"
    collector = TraceCollector(trace_dir, DebugConfig(trace_retention_files=3))
    assert collector.config.enabled is False
    assert collector.enabled is True  # env var override

    # An injected env mapping isolates a collector from os.environ.
    assert TraceCollector(trace_dir, DebugConfig(), env={}).enabled is False

    paths = []
    for tick in range(1, 6):
        collector.begin_tick(tick, f"2026-08-18T00:00:0{tick}+00:00")
        collector.record_tool("mechanic", "heal.repair", 1.25, True)
        collector.record_agent("trader", 3.5, 2)
        collector.record_comms(1, 0, 0.75)
        path = collector.end_tick({"tick": tick, "duration_ms": 9.0})
        assert path is not None
        assert path.name == f"trace_tick_{tick:08d}.jsonl"
        paths.append(path)

    # Retention keeps only the newest 3 files.
    remaining = sorted(p.name for p in trace_dir.glob("trace_tick_*.jsonl"))
    assert remaining == [
        "trace_tick_00000003.jsonl",
        "trace_tick_00000004.jsonl",
        "trace_tick_00000005.jsonl",
    ]
    assert [p.name for p in collector.latest(2)] == [
        "trace_tick_00000005.jsonl",
        "trace_tick_00000004.jsonl",
    ]

    lines = paths[-1].read_text().splitlines()
    assert len(lines) == 4  # summary first, then one line per recorded event
    summary = json.loads(lines[0])
    assert summary["tick"] == 5
    assert summary["duration_ms"] == 9.0
    assert summary["started_ts"] == "2026-08-18T00:00:05+00:00"
    assert collector.read_summary(paths[-1]) == summary
    tool_event, agent_event, comms_event = (json.loads(line) for line in lines[1:])
    assert tool_event == {
        "event": "tool", "caller": "mechanic", "tool": "heal.repair",
        "ms": 1.25, "ok": True, "error": None,
    }
    assert agent_event["event"] == "agent"
    assert agent_event["agent"] == "trader"
    assert agent_event["actions"] == 2
    assert comms_event == {"event": "comms", "processed": 1, "expired": 0, "ms": 0.75}


def test_trace_collector_truncates_traceback_tails(tmp_path):
    collector = TraceCollector(
        tmp_path / "trace", DebugConfig(enabled=True), env={}
    )
    collector.begin_tick(1, "2026-08-18T00:00:00+00:00")
    collector.record_agent("trader", 1.0, 0, error="boom", traceback_tail="x" * 5000)
    path = collector.end_tick({"tick": 1})
    event = json.loads(path.read_text().splitlines()[1])
    assert len(event["traceback_tail"]) == 800


# ------------------------------------------------------- registry statistics


def test_registry_stats_accumulate_and_tool_slow_fires_without_kwargs(tmp_path):
    world = sim_world(tmp_path, debug={"slow_tool_ms": 0.0001})

    ok = world.use_tool("hunter", "jobs.search", live=False)
    assert ok.ok
    bad = world.use_tool("hunter", "jobs.search", bogus_flag=True)
    assert not bad.ok

    snap = world.tools.stats_snapshot()
    entry = snap["jobs.search"]
    assert entry["calls"] == 2
    assert entry["errors"] == 1
    assert entry["total_ms"] > 0
    assert entry["avg_ms"] > 0
    assert entry["max_ms"] >= entry["avg_ms"]

    # Denials never execute, so they never enter the stats.
    denied = world.use_tool("hunter", "governance.freeze", target="closer", reason="no")
    assert not denied.ok
    assert "governance.freeze" not in world.tools.stats_snapshot()

    # Existing error accounting semantics are unchanged.
    assert (world.store.get_kv("tool_errors") or {})["jobs.search"] == 1

    slow = events_of(world, "tool_slow")
    assert len(slow) == 2  # the tiny threshold catches both executed calls
    for event in slow:
        assert set(event["payload"]) == {"tool", "ms"}  # no kwargs, no payloads
        assert event["payload"]["tool"] == "jobs.search"
        assert event["payload"]["ms"] >= 0


# -------------------------------------------------------- heartbeat efficiency


def test_heartbeat_pumps_inboxes_only_for_agents_with_queued_mail(
    tmp_path, monkeypatch
):
    # Roles are stubbed so nothing but the seeded ping ever queues bus mail
    # (e.g. the operator's quorum requests or mechanic health broadcasts).
    stub_all_roles(monkeypatch)

    pumped: list[str] = []

    def counting_pump(world_arg, agent, **kwargs):
        pumped.append(agent)
        return []

    monkeypatch.setattr("sovereign.comms.handlers.process_inbox", counting_pump)

    world = sim_world(tmp_path)
    step(world)
    assert pumped == []  # empty bus: zero process_inbox invocations

    world.comms.send("director", "trader", "ping", {}, now=world.now)
    step(world)
    assert pumped == ["trader"]  # exactly one call, only for that recipient


def test_tick_metrics_entries_carry_comms_and_agent_timings_when_traced(tmp_path):
    traced = sim_world(tmp_path / "traced", debug={"enabled": True})
    step(traced)
    entry = traced.store.get_kv(TICK_METRICS_KEY)[-1]
    assert entry["comms_ms"] >= 0
    agents_ms = entry["agents_ms"]
    assert 1 <= len(agents_ms) <= 5
    assert set(agents_ms) <= set(PIPELINE_AGENTS)
    assert all(ms >= 0 for ms in agents_ms.values())

    # Without tracing the ring keeps its legacy entry shape exactly.
    plain = sim_world(tmp_path / "plain")
    step(plain)
    assert set(plain.store.get_kv(TICK_METRICS_KEY)[-1]) == {
        "tick", "ms", "actions", "errors",
    }


def test_agent_exception_traceback_lives_in_trace_file_not_events(
    tmp_path, monkeypatch
):
    world = sim_world(tmp_path, debug={"enabled": True})

    def trader(w):
        raise RuntimeError("kaboom-J8F2-marker")

    monkeypatch.setattr("sovereign.agents.roles.trader", trader)
    report = step(world)
    assert report["errors"] == 1

    latest = world.debug_trace.latest(1)
    assert latest
    lines = latest[0].read_text().splitlines()
    agent_events = [
        json.loads(line)
        for line in lines[1:]
        if json.loads(line).get("event") == "agent"
    ]
    failed = next(e for e in agent_events if e["agent"] == "trader")
    assert failed["error"] == "kaboom-J8F2-marker"
    tail = failed["traceback_tail"]
    assert tail and len(tail) <= 800
    assert "kaboom-J8F2-marker" in tail
    assert "test_debug_efficiency" in tail  # the raising frame's file path

    # Events keep only the short error string — never the traceback.
    errors = events_of(world, "agent_error")
    assert errors and errors[-1]["payload"] == {"error": "kaboom-J8F2-marker"}
    events_blob = json.dumps(world.store.events(500))
    assert "kaboom-J8F2-marker" in events_blob
    assert "test_debug_efficiency" not in events_blob
    assert "traceback_tail" not in events_blob


def test_report_idle_flips_with_open_work(tmp_path, monkeypatch):
    stub_all_roles(monkeypatch)

    world = sim_world(tmp_path)
    quiet = step(world)
    assert quiet["idle"] is True  # no jobs, empty bus
    assert quiet["comms_ms"] >= 0

    world.store.upsert_job(
        {"id": "job_idle_1", "source": "manual", "title": "T", "status": "open",
         "price_usd": 25}
    )
    busy = step(world)
    assert busy["idle"] is False


# ------------------------------------------------------------- daemon throttle


def _offline_live_config(tmp_path) -> EngineConfig:
    """Deterministic offline live config: no claude binary, no market fetch."""
    return EngineConfig(
        mode="live",
        data_dir=tmp_path,
        fetch_market_data=False,
        tick_seconds=7.0,
        idle_tick_seconds=61.0,
        models={"claude_bin": "claude-bin-that-does-not-exist"},
    )  # type: ignore[arg-type]


def _fake_report(tick: int, idle: bool) -> dict[str, Any]:
    return {
        "tick": tick, "actions": 0, "duration_ms": 1.0, "errors": 0,
        "comms_ms": 0.0, "idle": idle, "equity": 0.0, "revenue": 0.0,
        "trailing": 0.0, "frozen": [], "pipeline": {},
    }


def test_daemon_sleeps_idle_interval_when_idle_and_tick_seconds_when_active(
    tmp_path, monkeypatch
):
    sleeps: list[float] = []
    monkeypatch.setattr(daemon_module.time, "sleep", lambda s: sleeps.append(s))
    calls = {"n": 0}

    def idle_step(world):
        calls["n"] += 1
        return _fake_report(calls["n"], idle=True)

    monkeypatch.setattr(daemon_module, "step", idle_step)
    serve(_offline_live_config(tmp_path / "idle"), ticks=2, force=True)
    assert sleeps == [61.0]  # max(tick_seconds, idle_tick_seconds)

    sleeps.clear()
    calls["n"] = 0

    def active_step(world):
        calls["n"] += 1
        return _fake_report(calls["n"], idle=False)

    monkeypatch.setattr(daemon_module, "step", active_step)
    serve(_offline_live_config(tmp_path / "active"), ticks=2, force=True)
    assert sleeps == [7.0]  # plain tick_seconds while active


# --------------------------------------------------------------- heal deep flag


class _SpyConn:
    """Delegating sqlite connection wrapper that records executed SQL."""

    def __init__(self, real):
        self._real = real
        self.sql: list[str] = []

    def execute(self, sql, *args, **kwargs):
        self.sql.append(str(sql))
        return self._real.execute(sql, *args, **kwargs)

    def __getattr__(self, name):
        return getattr(self._real, name)


def test_diagnose_runs_quick_check_only_when_deep(tmp_path, monkeypatch):
    world = sim_world(tmp_path)
    spy = _SpyConn(world.store.conn)
    monkeypatch.setattr(world.store, "conn", spy)

    shallow = diagnose(world, deep=False)
    assert all("quick_check" not in sql for sql in spy.sql)
    assert any("sqlite_master" in sql for sql in spy.sql)  # schema check always runs
    assert "sqlite" not in {finding.code for finding in shallow}

    spy.sql.clear()
    deep = diagnose(world, deep=True)
    assert any("quick_check" in sql for sql in spy.sql)
    by_code = {finding.code: finding for finding in deep}
    assert by_code["sqlite"].ok is True
    assert by_code["sqlite"].detail == "ok"


# ------------------------------------------------------------------ CLI: debug


def test_cli_debug_runs_traced_ticks_and_prints_report(tmp_path, capsys):
    code = main(["debug", "--data-dir", str(tmp_path), "--mode", "sim", "--ticks", "2"])
    out = capsys.readouterr().out
    assert code == 0
    payload = json.loads(out)
    assert set(payload) == {
        "ticks_run", "avg_tick_ms", "slowest_tools", "slowest_agents",
        "comms", "errors", "trace_files",
    }
    assert payload["ticks_run"] == 2
    assert payload["avg_tick_ms"] > 0
    assert payload["slowest_tools"]
    assert len(payload["slowest_tools"]) <= 8
    for tool in payload["slowest_tools"]:
        assert set(tool) == {"tool", "calls", "errors", "total_ms", "max_ms", "avg_ms"}
    ranked = [tool["total_ms"] for tool in payload["slowest_tools"]]
    assert ranked == sorted(ranked, reverse=True)
    assert payload["slowest_agents"]
    assert set(payload["slowest_agents"]) <= set(PIPELINE_AGENTS)
    assert isinstance(payload["comms"], dict)
    assert isinstance(payload["errors"], list)
    assert len(payload["trace_files"]) == 2
    assert all(Path(p).exists() for p in payload["trace_files"])

    # --show reads the newest trace summary without running any ticks.
    code = main(["debug", "--data-dir", str(tmp_path), "--mode", "sim", "--show"])
    shown = json.loads(capsys.readouterr().out)
    assert code == 0
    assert shown["trace_file"].endswith("trace_tick_00000002.jsonl")
    assert shown["summary"]["tick"] == 2
    assert {"duration_ms", "actions", "errors", "comms_ms", "tools"} <= set(shown["summary"])
