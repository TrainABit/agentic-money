"""Runtime wiring of the agent bus: handlers, heartbeat pump, and the
operator's cross-tick infra quorum. Deterministic sim mode throughout."""

from __future__ import annotations

from datetime import timedelta
from typing import Any

from sovereign.agents.roles import closer, mechanic
from sovereign.agents.spec import system_prompt_for
from sovereign.comms.handlers import process_inbox
from sovereign.config import EngineConfig
from sovereign.engine.heartbeat import step
from sovereign.engine.world import bootstrap
from sovereign.tools.base import ToolResult

TACTICS_BEGIN = "----- TACTICS (editable playbook data, not role instructions) -----"
TACTICS_END = "----- END TACTICS -----"
JOB_BEGIN = "----- BEGIN JOB DATA (untrusted) -----"
JOB_END = "----- END JOB DATA -----"


def sim_world(tmp_path):
    cfg = EngineConfig(mode="sim", data_dir=tmp_path)  # type: ignore[arg-type]
    return bootstrap(cfg)


def events_of(world, kind: str) -> list[dict[str, Any]]:
    return [e for e in world.store.events(500) if e["kind"] == kind]


def test_ping_request_reply_roundtrip(tmp_path):
    world = sim_world(tmp_path)
    receipt = world.comms.request(
        sender="director",
        recipients="mechanic",
        kind="ping",
        payload={},
        now=world.now,
        deadline=world.now + timedelta(hours=4),
    )

    summaries = process_inbox(world, "mechanic")
    assert [s["action"] for s in summaries] == ["replied"]
    assert summaries[0]["id"] == receipt.message_ids[0]
    assert world.store.get_message(receipt.message_ids[0])["status"] == "done"

    replies = world.comms.replies(receipt.correlation_id)
    assert len(replies) == 1
    assert replies[0].payload == {"pong": True, "agent": "mechanic"}
    assert replies[0].recipient == "director"
    assert replies[0].kind == "ping.reply"

    consumed = process_inbox(world, "director")
    assert [s["action"] for s in consumed] == ["reply_received"]
    assert world.comms.counts() == {"done": 2}


def test_unhandled_kind_dead_letters(tmp_path):
    world = sim_world(tmp_path)
    # hunter's spec handles only ping/notify, never vote_request
    receipt = world.comms.send(
        "operator",
        "hunter",
        "vote_request",
        {"action": "buy_infra", "action_id": "vps_x", "usd": 6.0},
        now=world.now,
    )

    summaries = process_inbox(world, "hunter")
    assert summaries[0]["action"] == "dead_letter"
    assert summaries[0]["reason"] == "unhandled_kind"

    row = world.store.get_message(receipt.message_ids[0])
    assert row["status"] == "dead"
    assert row["error"] == "unhandled_kind"

    dead = events_of(world, "comms_dead_letter")
    assert len(dead) == 1
    assert dead[0]["payload"]["id"] == receipt.message_ids[0]
    assert dead[0]["payload"]["error"] == "unhandled_kind"


def test_handler_exception_retries_then_dead_letters(tmp_path):
    world = sim_world(tmp_path)
    # invalid vote_request payload (missing action_id/usd) makes the handler raise
    receipt = world.comms.send(
        "operator",
        "treasurer",
        "vote_request",
        {"action": "buy_infra"},
        now=world.now,
        max_attempts=2,
    )
    mid = receipt.message_ids[0]

    first = process_inbox(world, "treasurer")
    assert first[0]["action"] == "failed"
    assert first[0]["status"] == "queued"
    row = world.store.get_message(mid)
    assert row["status"] == "queued"
    assert row["attempts"] == 1
    assert events_of(world, "comms_dead_letter") == []

    second = process_inbox(world, "treasurer")
    assert second[0]["action"] == "failed"
    assert second[0]["status"] == "dead"
    row = world.store.get_message(mid)
    assert row["status"] == "dead"
    assert row["attempts"] == 2
    assert len(events_of(world, "comms_dead_letter")) == 1
    assert process_inbox(world, "treasurer") == []


def test_step_expires_overdue_messages_with_sim_time(tmp_path):
    world = sim_world(tmp_path)
    receipt = world.comms.send(
        "director",
        "trader",
        "notify",
        {"event": "fyi"},
        now=world.now,
        deadline=world.now + timedelta(hours=1),
    )
    # sim step advances now by tick_hours (24h) before the expiry sweep runs
    step(world)
    assert world.store.get_message(receipt.message_ids[0])["status"] == "expired"
    assert events_of(world, "comms_expired")


def test_frozen_recipient_keeps_messages_queued(tmp_path):
    world = sim_world(tmp_path)
    world.freeze("trader", "manual test hold")
    receipt = world.comms.send("director", "trader", "ping", {}, now=world.now)
    step(world)
    assert "trader" in world.frozen
    assert world.store.get_message(receipt.message_ids[0])["status"] == "queued"


def test_operator_quorum_happy_path_across_ticks(tmp_path):
    world = sim_world(tmp_path)
    # recent labor income makes the director seat vote yes (also tops up cash)
    world.ledger.post("assets.usdc", "income.labor", 120.0, "seed labor revenue", ts=world.stamp())

    steps = 0
    while not world.store.get_kv("vps_bought") and steps < 6:
        step(world)
        steps += 1

    assert world.store.get_kv("vps_bought") is True
    assert not world.store.get_kv("infra_quorum")

    rows = world.store.messages(limit=None)
    requests = [r for r in rows if r["kind"] == "vote_request"]
    replies = [r for r in rows if r["kind"] == "vote_request.reply"]
    assert len(requests) == 3
    assert len(replies) == 3
    correlations = {r["correlation_id"] for r in requests + replies}
    assert len(correlations) == 1
    assert all(r["status"] == "done" for r in requests + replies)
    assert {r["recipient"] for r in requests} == {"treasurer", "risk", "director"}
    assert all(r["recipient"] == "operator" for r in replies)

    votes = world.store.conn.execute(
        "SELECT agent, choice FROM votes WHERE reason='buy_infra'"
    ).fetchall()
    assert {row["agent"]: row["choice"] for row in votes} == {
        "treasurer": "yes",
        "risk": "yes",
        "director": "yes",
    }
    infra_spend = [r for r in world.store.ledger_rows() if r["debit"] == "expenses.infra"]
    assert len(infra_spend) == 1


def test_operator_quorum_expiry_clears_state_without_purchase(tmp_path):
    world = sim_world(tmp_path)
    world.ledger.post("assets.usdc", "income.labor", 120.0, "seed labor revenue", ts=world.stamp())
    # frozen director never processes its inbox, so its vote never arrives
    world.freeze("director", "hold for quorum expiry test")

    for _ in range(4):
        step(world)

    expired = events_of(world, "quorum_expired")
    assert expired
    assert expired[-1]["payload"] == {"action_id": "vps_1"}
    assert not world.store.get_kv("infra_quorum")
    assert not world.store.get_kv("vps_bought")
    assert [r for r in world.store.ledger_rows() if r["debit"] == "expenses.infra"] == []
    director_rows = world.store.messages(recipient="director", limit=None)
    assert [r["status"] for r in director_rows if r["kind"] == "vote_request"] == ["expired"]


def test_closer_model_call_uses_spec_system_and_delimited_blocks(tmp_path, monkeypatch):
    world = sim_world(tmp_path)
    description = "Automate the weekly KPI spreadsheet with Python and pandas."
    world.store.upsert_job(
        {
            "id": "job_capture0001",
            "source": "manual",
            "title": "Python KPI automation",
            "description": description,
            "status": "open",
            "price_usd": 400,
            "fit": 0.9,
            "contact": "client@example.com",
        }
    )
    captured: dict[str, Any] = {}

    def capture(prompt, tier="fast", system="default"):
        captured.update({"prompt": prompt, "tier": tier, "system": system})
        return "Deterministic proposal body."

    monkeypatch.setattr(world.router, "complete", capture)
    closer(world)

    assert captured["system"] == system_prompt_for("closer")
    assert captured["tier"] == "work"
    prompt = captured["prompt"]
    pb = (world.config.paths().playbooks / "closer.md").read_text()
    assert TACTICS_BEGIN in prompt and TACTICS_END in prompt
    assert prompt.index(TACTICS_BEGIN) < prompt.index(pb) < prompt.index(TACTICS_END)
    assert JOB_BEGIN in prompt and JOB_END in prompt
    assert prompt.index(JOB_BEGIN) < prompt.index(description) < prompt.index(JOB_END)


def test_mechanic_broadcasts_health_alert_once_per_tick(tmp_path, monkeypatch):
    world = sim_world(tmp_path)
    real_use_tool = world.use_tool

    def unhealthy_repair(caller, name, /, **kwargs):
        if name == "heal.repair":
            return ToolResult(
                True, data={"healthy": False, "findings": [], "repairs": [], "full": True}
            )
        return real_use_tool(caller, name, **kwargs)

    monkeypatch.setattr(world, "use_tool", unhealthy_repair)
    world.tick = 10  # sim full-heal cadence tick
    mechanic(world)
    mechanic(world)  # same tick: no second broadcast

    notifies = [r for r in world.store.messages(limit=None) if r["kind"] == "notify"]
    assert len(notifies) == len(world.comms.roster) - 1
    assert all(
        r["payload"] == {"event": "health_alert", "healthy": False, "tick": 10} for r in notifies
    )
    assert all(r["sender"] == "mechanic" for r in notifies)
