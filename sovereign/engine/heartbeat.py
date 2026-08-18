from __future__ import annotations

import time
import traceback
import uuid
from dataclasses import dataclass, field
from datetime import timedelta
from typing import Any, Callable

from sovereign.engine.watchdog import AgentTimeout, run_with_timeout
from sovereign.engine.world import World, ensure_certified, load_prices
from sovereign.plays import PLAYS, attention_map, play_roi


AgentFn = Callable[[World], list[dict[str, Any]]]


@dataclass
class AgentTick:
    """Outcome of one named role inside a heartbeat tick."""

    name: str
    actions: list[dict[str, Any]] = field(default_factory=list)
    errors: int = 0
    ms: float = 0.0
    comms_ms: float = 0.0
    comms_processed: int = 0
    timeout: bool = False
    error: str | None = None
    skipped_frozen: bool = False

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


def _pump_inbox(world: World, agent: str, timeout_s: float) -> tuple[float, int]:
    """Timeout-guarded inbox pump. Returns (comms_ms, processed_count)."""
    from sovereign.comms.handlers import process_inbox

    started = time.monotonic()
    try:
        summaries = run_with_timeout(
            lambda: process_inbox(world, agent), seconds=timeout_s, agent=agent
        )
    except AgentTimeout:
        world.store.emit("comms_timeout", {"agent": agent}, agent)
        return (time.monotonic() - started) * 1000.0, 0
    comms_ms = (time.monotonic() - started) * 1000.0
    if summaries:
        world.store.emit("comms", {"count": len(summaries), "results": summaries}, agent)
    return comms_ms, len(summaries)


def run_one_agent(
    world: World,
    name: str,
    *,
    queued: dict[str, int] | None = None,
    timeout_s: float | None = None,
    tracing: bool = False,
) -> AgentTick:
    """Run one named role with watchdog, reputation, and event emission.

    Workers call this without ``start_tick`` / ``finish_tick``.
    """
    from sovereign.agents import roles

    timeout_s = float(timeout_s if timeout_s is not None else world.config.agent_timeout_seconds)
    queued = queued if queued is not None else {}
    fn = getattr(roles, name, None)
    if fn is None:
        return AgentTick(name=name, errors=1, error=f"unknown agent {name}")
    if name in world.frozen:
        world.store.emit("skipped_frozen", {"agent": name}, name)
        return AgentTick(name=name, skipped_frozen=True)
    result = AgentTick(name=name)
    if world.comms is not None and queued.get(name, 0) > 0:
        result.comms_ms, result.comms_processed = _pump_inbox(world, name, timeout_s)
    trace = getattr(world, "debug_trace", None)
    agent_started = time.monotonic()
    try:
        produced = run_with_timeout(
            lambda fn=fn: fn(world), seconds=timeout_s, agent=name
        ) or []
    except AgentTimeout:
        budget_ms = timeout_s * 1000.0
        result.ms = budget_ms
        result.errors = 1
        result.timeout = True
        result.error = f"timeout after {timeout_s:g}s"
        world.store.emit("agent_timeout", {"agent": name, "seconds": timeout_s}, name)
        if tracing and trace is not None:
            trace.record_agent(name, budget_ms, 0, error=result.error)
        world.reputation.slash(name, 3, result.error)
        if world.reputation.should_freeze(name):
            world.freeze(name, result.error, kind="runtime")
        return result
    except Exception as e:
        role_ms = (time.monotonic() - agent_started) * 1000.0
        result.ms = role_ms
        result.errors = 1
        result.error = str(e)
        world.store.emit("agent_error", {"error": str(e)}, name)
        if tracing and trace is not None:
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
        return result
    role_ms = (time.monotonic() - agent_started) * 1000.0
    result.ms = role_ms
    result.actions = list(produced)
    if tracing and trace is not None:
        trace.record_agent(name, role_ms, len(produced))
    for action in produced:
        world.store.emit(action.get("kind", "action"), action, name)
    return result


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
    from sovereign.engine.workers import PIPELINE_NAMES, WAVES, WorkerPool

    # One aggregate query decides whose inbox gets pumped this tick; agents
    # with nothing queued skip process_inbox entirely.
    queued = world.store.queued_recipient_counts() if world.comms is not None else {}
    timeout_s = world.config.agent_timeout_seconds

    def execute(name: str) -> AgentTick:
        tick = run_one_agent(
            world,
            name,
            queued=queued,
            timeout_s=timeout_s,
            tracing=tracing,
        )
        agent_ms[name] = agent_ms.get(name, 0.0) + tick.ms
        actions.extend(tick.actions)
        nonlocal_errors[0] += tick.errors
        comms_acc[0] += tick.comms_ms
        comms_acc[1] += tick.comms_processed
        return tick

    nonlocal_errors = [0]
    comms_acc = [0.0, 0]
    if world.config.workers.enabled:
        with WorkerPool(world.config) as pool:
            for wave in WAVES:
                names = tuple(name for name in wave if name in PIPELINE_NAMES)
                for row in pool.run_wave(names, world, execute):
                    if row.get("in_process"):
                        continue
                    if not row.get("ok"):
                        nonlocal_errors[0] += int(row.get("errors") or 1)
                        err = str(row.get("error") or "worker_failed")
                        world.store.emit(
                            "agent_error",
                            {"error": err, "worker": True},
                            str(row.get("agent") or "worker"),
                        )
    else:
        for name in PIPELINE_NAMES:
            execute(name)
    error_count = nonlocal_errors[0]
    comms_ms += comms_acc[0]
    comms_processed += comms_acc[1]

    # The end-of-tick snapshot doubles as the idle signal and as the gate for
    # one same-tick catch-up pump: mail sent during the tick to an inbox that
    # was empty at the start keeps its pre-gating delivery latency. Agents
    # already pumped are excluded so retries stay one-attempt-per-tick, and
    # frozen agents are still skipped.
    queued_after = world.store.queued_recipient_counts() if world.comms is not None else {}
    if world.comms is not None:
        late = [
            name
            for name in PIPELINE_NAMES
            if name not in world.frozen
            and queued_after.get(name, 0) > 0
            and queued.get(name, 0) == 0
        ]
        for name in late:
            extra_ms, extra_n = _pump_inbox(world, name, timeout_s)
            comms_ms += extra_ms
            comms_processed += extra_n
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
