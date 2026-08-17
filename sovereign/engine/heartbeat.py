from __future__ import annotations

import uuid
from typing import Any, Callable

from sovereign.engine.world import World, ensure_certified, load_prices
from sovereign.memory.playbooks import read_playbook
from sovereign.memory.store import iso
from sovereign.plays import PLAYS, attention_map, play_roi
from datetime import timedelta


AgentFn = Callable[[World], list[dict[str, Any]]]


def _mid() -> str:
    return "m_" + uuid.uuid4().hex[:10]


def step(world: World) -> dict[str, Any]:
    world.tick += 1
    if world.config.mode == "sim":
        world.now = world.now + timedelta(hours=world.config.tick_hours)
    else:
        from sovereign.memory.store import utcnow

        world.now = utcnow()
    load_prices(world)
    ensure_certified(world)

    from sovereign.channels.replies import consume as consume_replies
    from sovereign.channels import mail as mailbox
    from sovereign.labor.pipeline import accept_job, reject_job
    from sovereign.capital.invoice import collect

    consume_replies(world)
    for msg in mailbox.ingest_dropins(world):
        parsed = mailbox.interpret(msg)
        if not parsed:
            continue
        try:
            if parsed["action"] == "accept":
                accept_job(world, parsed["job_id"], source="mail")
            elif parsed["action"] == "reject":
                reject_job(world, parsed["job_id"], source="mail")
            elif parsed["action"] == "paid":
                collect(world, parsed["job_id"], source="mail")
        except KeyError:
            pass
        msg["status"] = "read"
        world.store.upsert_mail(msg)

    actions: list[dict[str, Any]] = []
    from sovereign.agents import roles

    pipeline = [
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
        try:
            produced = fn(world) or []
        except Exception as e:
            world.store.emit("agent_error", {"error": str(e)}, name)
            world.reputation.slash(name, 5, f"exception: {e}")
            if world.reputation.should_freeze(name):
                world.frozen.add(name)
            continue
        actions.extend(produced)
        for a in produced:
            world.store.emit(a.get("kind", "action"), a, name)

    world.persist_kv()
    snap = world.ledger.snapshot(now=world.now)
    return {
        "tick": world.tick,
        "actions": len(actions),
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
                "created_ts": iso(),
                "attention": share,
            }
            world.store.upsert_mission(mission)
            created.append(mission)
    return created


def playbook(world: World, agent: str) -> str:
    return read_playbook(world.config.paths().playbooks, agent)
