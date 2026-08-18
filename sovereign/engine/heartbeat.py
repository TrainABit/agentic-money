from __future__ import annotations

import time
import uuid
from datetime import timedelta
from typing import Any, Callable

from sovereign.engine.world import World, ensure_certified, load_prices
from sovereign.plays import PLAYS, attention_map, play_roi


AgentFn = Callable[[World], list[dict[str, Any]]]

TICK_METRICS_KEY = "tick_metrics"
TICK_METRICS_KEEP = 50


def _mid() -> str:
    return "m_" + uuid.uuid4().hex[:10]


def _record_tick_metrics(world: World, elapsed_ms: float, actions: int, errors: int) -> None:
    ring = world.store.get_kv(TICK_METRICS_KEY)
    if not isinstance(ring, list):
        ring = []
    ring.append(
        {
            "tick": world.tick,
            "ms": round(elapsed_ms, 1),
            "actions": actions,
            "errors": errors,
        }
    )
    world.store.set_kv(TICK_METRICS_KEY, ring[-TICK_METRICS_KEEP:])


def _maybe_write_weekly_report(world: World) -> None:
    due = world.scheduler.claim(
        "weekly_report",
        now=world.now,
        tick=world.tick,
        sim_every_ticks=7,
        live_every=timedelta(hours=168),
    )
    if not due or world.tick <= 1:
        return
    from sovereign import ops

    try:
        ops.write_weekly_report(world)
    except Exception as e:  # reporting must never crash a tick
        world.store.emit("report_error", {"error": str(e)[:200]}, "bookkeeper")


def step(world: World) -> dict[str, Any]:
    started = time.monotonic()
    world.start_tick()
    load_prices(world)
    ensure_certified(world)

    from sovereign.channels.replies import consume as consume_replies
    from sovereign.channels import mail as mailbox
    from sovereign.labor.pipeline import accept_job, reject_job

    consume_replies(world)
    for msg in mailbox.ingest_dropins(world):
        parsed = mailbox.interpret(msg)
        if not parsed:
            msg["status"] = "ignored"
            msg["processing_error"] = "no valid job reference"
            world.store.upsert_mail(msg)
            continue
        try:
            if parsed["action"] == "accept":
                accept_job(world, parsed["job_id"], source="mail")
            elif parsed["action"] == "reject":
                reject_job(world, parsed["job_id"], source="mail")
            elif parsed["action"] in {"paid", "paid_claim"}:
                world.store.emit("mail_paid_ignored", {"job_id": parsed["job_id"]}, "courier")
        except (KeyError, ValueError) as exc:
            msg["status"] = "error"
            msg["processing_error"] = str(exc)[:200]
            world.store.upsert_mail(msg)
            continue
        msg["status"] = "read"
        world.store.upsert_mail(msg)

    if world.comms is not None:
        world.comms.expire_due(now=world.now)
        if world.scheduler.claim(
            "comms_retention",
            now=world.now,
            tick=world.tick,
            sim_every_ticks=50,
            live_every=timedelta(hours=24),
        ):
            world.comms.prune(now=world.now, older_than_days=14.0)

    actions: list[dict[str, Any]] = []
    error_count = 0
    from sovereign.agents import roles
    from sovereign.comms.handlers import process_inbox

    pipeline = [
        roles.mechanic,
        roles.bookkeeper,
        roles.risk,
        roles.ethics,
        roles.director,
        roles.hunter,
        roles.closer,
        roles.crafter,
        roles.trader,
        roles.publisher,
        roles.scout,
        roles.operator,
        roles.treasurer,
        roles.auditor,
        roles.improver,
        roles.courier,
    ]
    for fn in pipeline:
        name = fn.__name__
        if name in world.frozen:
            world.store.emit("skipped_frozen", {"agent": name}, name)
            continue
        if world.comms is not None:
            summaries = process_inbox(world, name)
            if summaries:
                # Summaries carry ids/kinds/statuses only, never payload contents.
                world.store.emit("comms", {"count": len(summaries), "results": summaries}, name)
        try:
            produced = fn(world) or []
        except Exception as e:
            error_count += 1
            world.store.emit("agent_error", {"error": str(e)}, name)
            world.reputation.slash(name, 5, f"exception: {e}")
            if world.reputation.should_freeze(name):
                world.freeze(name, f"exception: {e}", kind="runtime")
            continue
        actions.extend(produced)
        for a in produced:
            world.store.emit(a.get("kind", "action"), a, name)

    world.persist_kv()
    world.finish_tick()
    elapsed_ms = (time.monotonic() - started) * 1000.0
    _record_tick_metrics(world, elapsed_ms, len(actions), error_count)
    _maybe_write_weekly_report(world)
    snap = world.ledger.snapshot(now=world.now)
    return {
        "tick": world.tick,
        "actions": len(actions),
        "duration_ms": round(elapsed_ms, 1),
        "errors": error_count,
        "equity": snap["equity_usd"],
        "revenue": snap["revenue_usd"],
        "trailing": snap["trailing_30d_usd"],
        "frozen": sorted(world.frozen),
        "pipeline": world.store.job_counts(),
    }


def fund_missions(world: World) -> list[dict[str, Any]]:
    snap = world.ledger.snapshot(now=world.now)
    roi = play_roi(world.store.outcomes(200))
    att = attention_map(
        snap["trailing_30d_usd"],
        world.config.goals.minimum_usd,
        world.config.goals.recommended_usd,
        roi=roi,
        override=world.store.get_kv("attention_override"),
    )
    existing = {m["play_id"] + ":" + m["agent"] for m in world.store.missions() if m.get("status") == "active"}
    created = []
    for play in PLAYS:
        share = att[play.id]
        if share < 0.02:
            continue
        for agent in play.agents[:3]:
            key = play.id + ":" + agent
            if key in existing:
                continue
            mission = {
                "id": _mid(),
                "play_id": play.id,
                "agent": agent,
                "title": f"{play.title} / {agent}",
                "status": "active",
                "budget_usd": round(50 * share * 10, 2),
                "created_ts": world.stamp(),
                "attention": share,
            }
            world.store.upsert_mission(mission)
            created.append(mission)
    return created


def playbook(world: World, agent: str, job_id: str | None = None) -> str:
    from sovereign.memory.playbooks import read_playbook_ab

    return read_playbook_ab(world, agent, job_id)
