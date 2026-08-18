"""Tick metrics ring, ops.metrics / weekly reports, and dashboard observability.

Deterministic sim mode throughout; the dashboard tests exercise the real
FastAPI app over the same data dir the engine wrote.
"""

from __future__ import annotations

import json

import pytest
from fastapi.testclient import TestClient

from sovereign import ops
from sovereign.comms.bus import Message
from sovereign.config import EngineConfig
from sovereign.dashboard.app import HTML, create_app
from sovereign.engine.heartbeat import TICK_METRICS_KEEP, TICK_METRICS_KEY, step
from sovereign.engine.world import bootstrap

SECRET = "PAYLOAD_MARKER_c9f2e1_never_leaves_the_store"


def sim_world(tmp_path):
    cfg = EngineConfig(mode="sim", data_dir=tmp_path)  # type: ignore[arg-type]
    return bootstrap(cfg)


def events_of(world, kind: str):
    return [e for e in world.store.events(500) if e["kind"] == kind]


def test_tick_metrics_ring_is_bounded_and_report_carries_duration(tmp_path):
    world = sim_world(tmp_path)
    reports = [step(world) for _ in range(3)]

    ring = world.store.get_kv(TICK_METRICS_KEY)
    assert isinstance(ring, list)
    assert len(ring) == 3
    assert [entry["tick"] for entry in ring] == [1, 2, 3]
    for entry in ring:
        assert set(entry) == {"tick", "ms", "actions", "errors"}
        assert entry["ms"] > 0
        assert entry["errors"] == 0
    for report in reports:
        assert report["duration_ms"] > 0
        assert report["errors"] == 0

    # The ring keeps only the newest TICK_METRICS_KEEP entries.
    world.store.set_kv(
        TICK_METRICS_KEY,
        [{"tick": -i, "ms": 1.0, "actions": 0, "errors": 0} for i in range(60)],
    )
    step(world)
    ring = world.store.get_kv(TICK_METRICS_KEY)
    assert len(ring) == TICK_METRICS_KEEP == 50
    assert ring[-1]["tick"] == world.tick


def test_agent_exception_is_counted_in_ring_and_report(tmp_path, monkeypatch):
    world = sim_world(tmp_path)

    def trader(w):
        raise RuntimeError("injected trader failure")

    monkeypatch.setattr("sovereign.agents.roles.trader", trader)
    report = step(world)

    assert report["errors"] == 1
    ring = world.store.get_kv(TICK_METRICS_KEY)
    assert ring[-1]["errors"] == 1
    errors = events_of(world, "agent_error")
    assert errors and errors[-1]["agent"] == "trader"


def test_metrics_shape_and_recent_error_aggregation(tmp_path):
    world = sim_world(tmp_path / "engine")
    for _ in range(2):
        step(world)
    world.store.emit("agent_error", {"error": "boom-1"}, "trader")
    world.store.emit("agent_error", {"error": "boom-2"}, "trader")
    world.store.emit("agent_error", {"error": "boom-3"}, "closer")

    m = ops.metrics(world)
    assert m["tick"] == world.tick
    assert m["mode"] == "sim"
    ring = world.store.get_kv(TICK_METRICS_KEY)
    assert m["ticks"]["recent"] == ring[-10:]
    assert m["ticks"]["last_ms"] == ring[-1]["ms"]
    assert m["ticks"]["avg_ms"] == pytest.approx(
        sum(entry["ms"] for entry in ring) / len(ring), abs=0.01
    )
    assert m["comms"] == world.comms.counts()
    assert m["pipeline"] == world.store.job_counts()
    assert set(m["revenue"]) == {"trailing_30d_usd", "lifetime_usd", "equity_usd"}
    assert m["agents"]["frozen"] == sorted(world.frozen)
    assert m["agents"]["recent_errors"] == {"trader": 2, "closer": 1}
    assert m["cognition"]["provider"] == "sim-brain"
    json.dumps(m)  # the whole snapshot is JSON-serializable

    fresh = sim_world(tmp_path / "fresh")
    assert ops.metrics(fresh)["ticks"] == {"recent": [], "avg_ms": 0, "last_ms": 0}


def test_write_weekly_report_sections_and_idempotency(tmp_path):
    world = sim_world(tmp_path)
    for _ in range(2):
        step(world)
    world.freeze("trader", "manual test hold")

    path = ops.write_weekly_report(world)
    iso_year, iso_week, _ = world.now.isocalendar()
    assert path.name == f"week_{iso_year}-W{iso_week:02d}.md"
    assert path.parent == world.config.paths().artifacts / "reports"
    text = path.read_text()
    assert f"# Weekly report — {world.config.firm_name}" in text
    for section in (
        "## Revenue",
        "## Pipeline",
        "## Invoices",
        "## Comms health",
        "## Incidents",
        "## Strategies",
        "## Goals progress",
    ):
        assert section in text
    assert "Trailing 30d" in text
    assert "| dead |" in text
    assert "trader (manual)" in text
    assert world.store.get_kv("last_weekly_report") == f"artifacts/reports/{path.name}"

    # Same week overwrites the same file instead of accumulating copies.
    again = ops.write_weekly_report(world)
    assert again == path
    assert list(path.parent.glob("*.md")) == [path]


def test_heartbeat_writes_weekly_report_automatically_by_tick_8(tmp_path):
    world = sim_world(tmp_path)
    reports_dir = world.config.paths().artifacts / "reports"

    for _ in range(8):
        step(world)
        if world.tick < 7:
            assert not list(reports_dir.glob("week_*.md"))

    files = list(reports_dir.glob("week_*.md"))
    assert len(files) == 1
    assert world.store.get_kv("last_weekly_report") == f"artifacts/reports/{files[0].name}"


def test_weekly_report_failure_never_crashes_a_tick(tmp_path, monkeypatch):
    world = sim_world(tmp_path)

    def explode(w):
        raise RuntimeError("disk full injected")

    monkeypatch.setattr("sovereign.ops.write_weekly_report", explode)
    reports = [step(world) for _ in range(8)]

    assert all(isinstance(report, dict) for report in reports)
    assert reports[-1]["tick"] == 8
    failures = events_of(world, "report_error")
    assert failures
    assert "disk full injected" in failures[-1]["payload"]["error"]
    assert not list((world.config.paths().artifacts / "reports").glob("*.md"))


def test_dashboard_metrics_and_comms_endpoints_on_loopback_default(tmp_path, monkeypatch):
    monkeypatch.delenv("SOVEREIGN_DASHBOARD_TOKEN", raising=False)
    world = sim_world(tmp_path)
    step(world)
    world.comms.send("director", "trader", "notify", {"event": SECRET}, now=world.now)
    receipt = world.comms.send("operator", "hunter", "notify", {"event": SECRET}, now=world.now)
    queued = Message.from_record(world.store.get_message(receipt.message_ids[0]))
    world.comms.dead_letter(queued, "manual dead-letter for test", now=world.now)

    client = TestClient(create_app(str(tmp_path), "sim"))

    m = client.get("/api/metrics")
    assert m.status_code == 200
    body = m.json()
    assert body["mode"] == "sim"
    assert body["ticks"]["recent"]
    assert body["comms"].get("dead") == 1
    assert set(body) >= {"tick", "ticks", "comms", "pipeline", "revenue", "agents", "cognition"}

    c = client.get("/api/comms")
    assert c.status_code == 200
    rows = c.json()
    assert rows
    assert all(
        set(row) == {"id", "ts", "kind", "sender", "recipient", "status", "attempts", "error"}
        for row in rows
    )
    assert SECRET not in c.text

    d = client.get("/api/comms", params={"status": "dead", "limit": 5})
    assert d.status_code == 200
    dead_rows = d.json()
    assert [row["status"] for row in dead_rows] == ["dead"]
    assert dead_rows[0]["id"] == receipt.message_ids[0]
    assert SECRET not in d.text

    assert client.get("/api/comms", params={"status": "bogus"}).status_code == 400
    assert client.get("/api/comms", params={"limit": 0}).status_code == 400
    assert client.get("/api/comms", params={"limit": 201}).status_code == 400
    assert client.get("/api/comms", params={"limit": "abc"}).status_code == 422


def test_dashboard_new_endpoints_require_bearer_token_when_set(tmp_path, monkeypatch):
    sim_world(tmp_path)
    monkeypatch.setenv("SOVEREIGN_DASHBOARD_TOKEN", "observer-secret")
    client = TestClient(create_app(str(tmp_path), "sim"))

    for path in ("/api/metrics", "/api/comms"):
        assert client.get(path).status_code == 401
        assert client.get(path, headers={"Authorization": "Bearer wrong"}).status_code == 401
        ok = client.get(path, headers={"Authorization": "Bearer observer-secret"})
        assert ok.status_code == 200


def test_dashboard_runtime_panel_present_and_safe():
    assert 'id="runtime"' in HTML
    assert 'id="agent-errors"' in HTML
    assert 'id="dead-letters"' in HTML
    assert "/api/metrics" in HTML
    assert "/api/comms?status=dead" in HTML
    assert "innerHTML" not in HTML
    assert "insertAdjacentHTML" not in HTML
