from __future__ import annotations

import time
import traceback
import uuid
from datetime import timedelta
from typing import Any, Callable

from sovereign.engine.watchdog import AgentTimeout, run_with_timeout
from sovereign.engine.world import World, ensure_certified, load_prices
from sovereign.plays import PLAYS, attention_map, play_roi


AgentFn = Callable[[World], list[dict[str, Any]]]

TICK_METRICS_KEY = "tick_metrics"
TICK_METRICS_KEEP = 50
# Job statuses that count as active work for the report's idle signal.
ACTIVE_JOB_STATUSES = ("open", "accepted", "in_progress", "delivered", "invoiced")
# How many of the slowest agents each traced ring entry names.
AGENTS_MS_TOP = 5


def _mid() -> str:
    return "m_" + uuid.uuid4().hex[:10]


def _slowest_agents(agent_ms: dict[str, float], top: int = AGENTS_MS_TOP) -> dict[str, float]:
    ranked = sorted(agent_ms.items(), key=lambda item: item[1], reverse=True)[:top]
    return {name: round(ms, 1) for name, ms in ranked}


def _tool_stats_delta(
    before: dict[str, dict[str, float]], after: dict[str, dict[str, float]]
) -> dict[str, dict[str, float]]:
    """Per-tool call/error/total_ms growth between two stats snapshots."""
    delta: dict[str, dict[str, float]] = {}
    for name, snap in after.items():
        prev = before.get(name, {})
        calls = int(snap["calls"]) - int(prev.get("calls", 0))
        if calls <= 0:
            continue
        delta[name] = {
            "calls": calls,
            "errors": int(snap["errors"]) - int(prev.get("errors", 0)),
            "total_ms": round(float(snap["total_ms"]) - float(prev.get("total_ms", 0.0)), 3),
        }
    return delta


def _record_tick_metrics(
    world: World,
    elapsed_ms: float,
    actions: int,
    errors: int,
    comms_ms: float | None = None,
    agents_ms: dict[str, float] | None = None,
) -> None:
    ring = world.store.get_kv(TICK_METRICS_KEY)
    if not isinstance(ring, list):
        ring = []
    entry: dict[str, Any] = {
        "tick": world.tick,
        "ms": round(elapsed_ms, 1),
        "actions": actions,
        "errors": errors,
    }
    # Extended keys appear only while tracing so untraced rings keep the
    # legacy entry shape byte-for-byte.
    if comms_ms is not None:
        entry["comms_ms"] = round(comms_ms, 1)
    if agents_ms is not None:
        entry["agents_ms"] = agents_ms
    ring.append(entry)
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
    trace = getattr(world, "debug_trace", None)
    tracing = trace is not None and trace.enabled
    tools_before = (
        world.tools.stats_snapshot() if tracing and world.tools is not None else {}
    )
    world.start_tick()
    if tracing:
        trace.begin_tick(world.tick, world.stamp())
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

    comms_ms = 0.0
    comms_processed = 0
    comms_expired = 0
    if world.comms is not None:
        comms_started = time.monotonic()
        comms_expired = world.comms.expire_due(now=world.now)
        if world.scheduler.claim(
            "comms_retention",
            now=world.now,
            tick=world.tick,
            sim_every_ticks=50,
            live_every=timedelta(hours=24),
        ):
            world.comms.prune(now=world.now, older_than_days=14.0)
        comms_ms += (time.monotonic() - comms_started) * 1000.0

    actions: list[dict[str, Any]] = []
    error_count = 0
    agent_ms: dict[str, float] = {}
    from sovereign.agents import roles
    from sovereign.comms.handlers import process_inbox

    # One aggregate query decides whose inbox gets pumped this tick; agents
    # with nothing queued skip process_inbox entirely.
    queued = world.store.queued_recipient_counts() if world.comms is not None else {}
    timeout_s = world.config.agent_timeout_seconds

    def pump_inbox(agent: str) -> None:
        """Timeout-guarded inbox pump: a wedged handler is abandoned with a
        "comms_timeout" event instead of hanging the tick."""
        nonlocal comms_ms, comms_processed
        comms_started = time.monotonic()
        try:
            summaries = run_with_timeout(
                lambda: process_inbox(world, agent), seconds=timeout_s, agent=agent
            )
        except AgentTimeout:
            comms_ms += (time.monotonic() - comms_started) * 1000.0
            world.store.emit("comms_timeout", {"agent": agent}, agent)
            return
        comms_ms += (time.monotonic() - comms_started) * 1000.0
        comms_processed += len(summaries)
        if summaries:
            # Summaries carry ids/kinds/statuses only, never payload contents.
            world.store.emit("comms", {"count": len(summaries), "results": summaries}, agent)

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
        if world.comms is not None and queued.get(name, 0) > 0:
            pump_inbox(name)
        agent_started = time.monotonic()
        try:
            # fn is bound as a lambda default so an abandoned worker that has
            # not started yet can never late-bind to a later role in this loop.
            produced = run_with_timeout(
                lambda fn=fn: fn(world), seconds=timeout_s, agent=name
            ) or []
        except AgentTimeout:
            # The wedged worker is abandoned (see watchdog docstring); the
            # tick moves on. Elapsed is recorded as the full budget.
            budget_ms = timeout_s * 1000.0
            agent_ms[name] = agent_ms.get(name, 0.0) + budget_ms
            error_count += 1
            world.store.emit("agent_timeout", {"agent": name, "seconds": timeout_s}, name)
            if tracing:
                trace.record_agent(name, budget_ms, 0, error=f"timeout after {timeout_s:g}s")
            world.reputation.slash(name, 3, f"timeout after {timeout_s:g}s")
            if world.reputation.should_freeze(name):
                world.freeze(name, f"timeout after {timeout_s:g}s", kind="runtime")
            continue
        except Exception as e:
            role_ms = (time.monotonic() - agent_started) * 1000.0
            agent_ms[name] = agent_ms.get(name, 0.0) + role_ms
            error_count += 1
            # Events keep only the short error string; the traceback tail
            # goes to the trace file and nowhere else.
            world.store.emit("agent_error", {"error": str(e)}, name)
            if tracing:
                trace.record_agent(
                    name,
                    role_ms,
                    0,
                    error=str(e),
                    traceback_tail=traceback.format_exc()[-800:],
                )
            world.reputation.slash(name, 5, f"exception: {e}")
            if world.reputation.should_freeze(name):
                world.freeze(name, f"exception: {e}", kind="runtime")
            continue
        role_ms = (time.monotonic() - agent_started) * 1000.0
        agent_ms[name] = agent_ms.get(name, 0.0) + role_ms
        if tracing:
            trace.record_agent(name, role_ms, len(produced))
        actions.extend(produced)
        for a in produced:
            world.store.emit(a.get("kind", "action"), a, name)

    # The end-of-tick snapshot doubles as the idle signal and as the gate for
    # one same-tick catch-up pump: mail sent during the tick to an inbox that
    # was empty at the start keeps its pre-gating delivery latency. Agents
    # already pumped are excluded so retries stay one-attempt-per-tick, and
    # frozen agents are still skipped.
    queued_after = world.store.queued_recipient_counts() if world.comms is not None else {}
    if world.comms is not None:
        late = [
            fn.__name__
            for fn in pipeline
            if fn.__name__ not in world.frozen
            and queued_after.get(fn.__name__, 0) > 0
            and queued.get(fn.__name__, 0) == 0
        ]
        for name in late:
            pump_inbox(name)
        if late:
            queued_after = world.store.queued_recipient_counts()

    world.persist_kv()
    world.finish_tick()
    elapsed_ms = (time.monotonic() - started) * 1000.0
    _record_tick_metrics(
        world,
        elapsed_ms,
        len(actions),
        error_count,
        comms_ms=comms_ms if tracing else None,
        agents_ms=_slowest_agents(agent_ms) if tracing else None,
    )
    if tracing:
        trace.record_comms(comms_processed, comms_expired, comms_ms)
        tools_after = (
            world.tools.stats_snapshot() if world.tools is not None else {}
        )
        trace.end_tick(
            {
                "tick": world.tick,
                "duration_ms": round(elapsed_ms, 1),
                "actions": len(actions),
                "errors": error_count,
                "comms_ms": round(comms_ms, 1),
                "tools": _tool_stats_delta(tools_before, tools_after),
            }
        )
    _maybe_write_weekly_report(world)
    snap = world.ledger.snapshot(now=world.now)
    job_counts = world.store.job_counts()
    idle = (
        not any(job_counts.get(status, 0) for status in ACTIVE_JOB_STATUSES)
        and sum(queued_after.values()) == 0
    )
    return {
        "tick": world.tick,
        "actions": len(actions),
        "duration_ms": round(elapsed_ms, 1),
        "errors": error_count,
        "comms_ms": round(comms_ms, 1),
        "idle": idle,
        "equity": snap["equity_usd"],
        "revenue": snap["revenue_usd"],
        "trailing": snap["trailing_30d_usd"],
        "frozen": sorted(world.frozen),
        "pipeline": job_counts,
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
