"""Operational readiness, runtime metrics, and periodic reporting.

:func:`readiness` runs local, side-effect-free probes (no network calls) and
returns a machine-readable report. Required checks gate the ``sovereign
bootstrap`` exit code; informational checks surface state a human should know
about (legacy wallets, certification coverage, comms backlog) without
blocking a healthy engine.

:func:`healthcheck` wraps :func:`readiness` with an optional last-tick
staleness bound into a single never-raising verdict — the exec probe behind
``sovereign healthcheck`` (Docker HEALTHCHECK, Kubernetes exec probes).

:func:`metrics` is the read-only runtime snapshot behind the dashboard,
:func:`write_weekly_report` renders the ISO-week markdown operations report,
and :func:`sanitized_messages` lists bus messages without their payloads.
"""

from __future__ import annotations

import importlib.util
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, TYPE_CHECKING

from sovereign.agents.spec import roster, tool_matrix
from sovereign.comms.bus import (
    STATUS_DEAD,
    STATUS_DONE,
    STATUS_EXPIRED,
    STATUS_QUEUED,
    STATUSES,
    Bus,
)
from sovereign.engine.heartbeat import TICK_METRICS_KEY
from sovereign.engine.schedule import aware_utc, parse_datetime
from sovereign.heal.checks import diagnose

if TYPE_CHECKING:
    from sovereign.engine.world import World

__all__ = [
    "healthcheck",
    "maintain",
    "metrics",
    "readiness",
    "sanitized_messages",
    "write_weekly_report",
]

# Surfaced as a dedicated informational check instead: a legacy Solana wallet
# is a preserved configuration, not a blocker, so it must not fail readiness
# through engine_health either.
_INFORMATIONAL_FINDINGS = frozenset({"wallet_backup"})


def _check(name: str, ok: bool, required: bool, detail: Any) -> dict[str, Any]:
    return {"name": name, "ok": bool(ok), "required": bool(required), "detail": detail}


def _bus_for(world: "World") -> tuple[Bus | None, bool, str | None]:
    """The world's bus, or a temporary one over the same store and roster.

    Worlds built by ``bootstrap()`` carry ``world.comms``; the fallback keeps
    the self-test meaningful on worlds constructed before the bus is wired,
    exercising the same messages table either way.
    """
    bus = getattr(world, "comms", None)
    if bus is not None:
        return bus, False, None
    try:
        return Bus(world.store, roster()), True, None
    except Exception as exc:  # pragma: no cover - store-level breakage
        return None, True, str(exc)[:200]


def _comms_roundtrip(world: "World", bus: Bus) -> tuple[bool, str]:
    """Send a ping from mechanic to mechanic, read it back, ack it.

    Never leaves queued residue: any probe row still queued after the test
    (success or failure) is acked in cleanup.
    """
    now = world.now
    receipt = None
    try:
        receipt = bus.send("mechanic", "mechanic", "ping", {"probe": "readiness"}, now=now)
        message_id = receipt.message_ids[0]
        inbox = bus.inbox("mechanic", now=now, limit=200)
        if not any(message.id == message_id for message in inbox):
            return False, "ping was sent but did not appear in the mechanic inbox"
        acked = bus.ack(message_id, now=now)
        if acked.status != "done":
            return False, f"ack left the ping in status {acked.status!r}"
        return True, "ping sent, delivered, and acked"
    except Exception as exc:
        return False, f"roundtrip failed: {exc}"[:200]
    finally:
        if receipt is not None:
            for message_id in receipt.message_ids:
                try:
                    record = world.store.get_message(message_id)
                    if record is not None and record.get("status") == STATUS_QUEUED:
                        bus.ack(message_id, now=now)
                except Exception:
                    pass


def readiness(world: "World") -> dict[str, Any]:
    """Local go/no-go report: ``ready`` is true iff every required check is ok."""
    checks: list[dict[str, Any]] = []

    version = ".".join(str(part) for part in sys.version_info[:3])
    checks.append(_check("python_version", sys.version_info >= (3, 11), True, version))

    findings = diagnose(world, deep=True)
    failing = [f.code for f in findings if not f.ok and f.code not in _INFORMATIONAL_FINDINGS]
    checks.append(
        _check(
            "engine_health",
            not failing,
            True,
            "all findings ok" if not failing else f"failing: {', '.join(failing)}",
        )
    )

    backup = next((f for f in findings if f.code == "wallet_backup"), None)
    checks.append(
        _check(
            "wallet_backup",
            backup is not None and backup.ok,
            False,
            backup.detail if backup is not None else "wallet_backup finding unavailable",
        )
    )

    tools = getattr(world, "tools", None)
    agents = sorted(roster())
    if tools is None or getattr(tools, "world", None) is None:
        checks.append(_check("tools_and_specs", False, True, "tool registry unbound"))
    else:
        matrix = tool_matrix()
        drifted = [
            agent
            for agent in agents
            if set(tools.available_to(agent))
            != {tool for tool, allowed in matrix.items() if agent in allowed}
        ]
        checks.append(
            _check(
                "tools_and_specs",
                not drifted,
                True,
                f"registry matches specs for all {len(agents)} agents"
                if not drifted
                else f"registry drifted from specs for: {', '.join(drifted)}",
            )
        )

    bus, borrowed, bus_error = _bus_for(world)
    if bus is None:
        checks.append(_check("comms_roundtrip", False, True, f"bus unavailable: {bus_error}"))
        checks.append(_check("comms_backlog", False, False, {}))
    else:
        ok, detail = _comms_roundtrip(world, bus)
        if ok and borrowed:
            detail += " (world.comms not wired; probed a temporary bus on the same store)"
        checks.append(_check("comms_roundtrip", ok, True, detail))
        counts = bus.counts()
        checks.append(
            _check(
                "comms_backlog",
                int(counts.get("dead", 0)) == 0 and int(counts.get("expired", 0)) == 0,
                False,
                counts,
            )
        )

    if world.config.mode == "sim":
        checks.append(_check("model_provider", True, False, "sim brain"))
    else:
        available = bool(world.router.claude.available())
        checks.append(
            _check(
                "model_provider",
                available,
                True,
                world.router.provider_name()
                if available
                else "claude CLI not on PATH — install Claude Code and `claude login`",
            )
        )

    certified = [report for report in world.certified if report.get("certified")]
    checks.append(
        _check("certification", bool(certified), False, f"{len(certified)} certified strategies")
    )

    checks.append(_check("web", True, False, _web_summary(world)))

    checks.append(_check("mcp", True, False, _mcp_summary(world)))

    ready = all(check["ok"] for check in checks if check["required"])
    return {"ready": ready, "mode": world.config.mode, "checks": checks}


def _web_summary(world: "World") -> dict[str, Any]:
    """Informational web-automation state: never gates readiness or health."""
    vault = getattr(world, "web_vault", None)
    try:
        vaulted = len(vault.list_domains()) if vault is not None else 0
    except Exception:
        vaulted = 0
    return {
        "enabled": bool(getattr(getattr(world, "web", None), "enabled", False)),
        "playwright": importlib.util.find_spec("playwright") is not None,
        "vaulted_sessions": vaulted,
    }


def _mcp_summary(world: "World") -> dict[str, Any]:
    """Informational MCP-bridge state: never gates readiness or health.

    Reads config plus the registry's cached servers()/errors() only — it must
    never call tools(), which connects and can spawn server subprocesses.
    """
    registry = getattr(world, "mcp", None)
    try:
        servers = (
            len(registry.servers())
            if registry is not None
            else len(getattr(getattr(world.config, "mcp", None), "servers", ()) or ())
        )
    except Exception:
        servers = 0
    try:
        errors = len(registry.errors()) if registry is not None else 0
    except Exception:
        errors = 0
    return {
        "enabled": bool(getattr(registry, "enabled", False)),
        "sdk": importlib.util.find_spec("mcp") is not None,
        "servers": servers,
        "errors": errors,
    }


def _probe_now(world: "World") -> datetime:
    """The later of the world's own time and the wall clock, tz-aware.

    ``world.now`` is authoritative for simulated worlds, where time runs far
    ahead of the wall clock. A probe process over a live data dir loads
    ``now`` from the last persisted tick, so there the world clock (real wall
    time) is what exposes a daemon that stopped ticking.
    """
    candidates: list[datetime] = []
    try:
        parsed = parse_datetime(getattr(world, "now", None))
        if parsed is not None:
            candidates.append(parsed)
    except Exception:
        pass
    try:
        clock = getattr(world, "clock", None)
        candidates.append(
            aware_utc(clock.now()) if clock is not None else datetime.now(timezone.utc)
        )
    except Exception:
        candidates.append(datetime.now(timezone.utc))
    return max(candidates)


def healthcheck(
    world: "World", *, max_staleness_seconds: float | None = None
) -> dict[str, Any]:
    """Exec-probe verdict for containers and orchestrators; never raises.

    Without ``max_staleness_seconds`` this is a readiness probe: ``ok``
    mirrors :func:`readiness`. With a bound it doubles as a liveness probe:
    ``ok`` additionally requires the newest recorded tick timestamp — the
    ``tick_start`` kv the daemon writes every tick, falling back to the
    ``meta`` kv — to be no older than the bound. A missing timestamp counts
    as stale only when a bound was requested. Any internal failure comes
    back as ``ok=False`` with the reason listed instead of an exception, so
    exit-code callers can always trust the verdict.
    """
    reasons: list[str] = []
    mode = "unknown"
    tick = 0
    ready = False
    stale = False
    last_tick_ts: str | None = None

    try:
        mode = str(world.config.mode)
        tick = int(getattr(world, "tick", 0) or 0)
    except Exception as exc:
        reasons.append(f"world state unreadable: {exc}"[:200])

    try:
        report = readiness(world)
        ready = bool(report.get("ready"))
        if not ready:
            failing = [
                str(check.get("name"))
                for check in report.get("checks", [])
                if check.get("required") and not check.get("ok")
            ]
            reasons.append(
                "failing required readiness checks: " + (", ".join(failing) or "unknown")
            )
    except Exception as exc:
        reasons.append(f"readiness probe failed: {exc}"[:200])

    try:
        marker = world.store.get_kv("tick_start")
        marker = marker if isinstance(marker, dict) else {}
        try:
            tick = max(tick, int(marker.get("tick")))
        except (TypeError, ValueError):
            pass
        newest: datetime | None = None
        for raw in (marker.get("started_ts"), marker.get("completed_ts")):
            parsed = parse_datetime(raw)
            if parsed is not None and (newest is None or parsed >= newest):
                newest, last_tick_ts = parsed, str(raw)
        if newest is None:
            meta = world.store.get_kv("meta")
            raw = meta.get("now") if isinstance(meta, dict) else None
            parsed = parse_datetime(raw)
            if parsed is not None:
                newest, last_tick_ts = parsed, str(raw)
        if max_staleness_seconds is not None:
            bound = float(max_staleness_seconds)
            if newest is None:
                stale = True
                reasons.append("no tick timestamp recorded yet (no tick_start or meta kv)")
            else:
                age = max(timedelta(0), _probe_now(world) - newest).total_seconds()
                if age > bound:
                    stale = True
                    reasons.append(
                        f"last tick at {last_tick_ts} is {age:.0f}s old (max {bound:g}s)"
                    )
    except Exception as exc:
        if max_staleness_seconds is not None:
            stale = True
        reasons.append(f"staleness check failed: {exc}"[:200])

    return {
        # reasons only ever carries failures, so a healthy probe stays
        # exactly `ready and not stale` while any internal error fails it.
        "ok": bool(ready and not stale and not reasons),
        "ready": ready,
        "mode": mode,
        "tick": tick,
        "last_tick_ts": last_tick_ts,
        "stale": stale,
        "reasons": reasons,
    }


def maintain(
    world: "World",
    *,
    vacuum: bool | None = None,
    comms_days: float | None = None,
) -> dict[str, Any]:
    """Prune retained rows and optionally compact the SQLite file.

    Event pruning uses the store's configured retention cap. Comms pruning
    deletes done/expired rows older than ``comms_days`` (default
    ``config.retention.comms_days``). VACUUM is skipped when
    ``config.retention.vacuum_on_maintain`` is false unless the caller
    overrides ``vacuum``.
    """
    retention = getattr(world.config, "retention", None)
    days = float(comms_days if comms_days is not None else getattr(retention, "comms_days", 30.0))
    do_vacuum = (
        bool(vacuum)
        if vacuum is not None
        else bool(getattr(retention, "vacuum_on_maintain", True))
    )
    world.store.prune_events()
    comms_pruned = 0
    bus = getattr(world, "comms", None)
    if bus is not None:
        comms_pruned = bus.prune(now=world.now, older_than_days=days)
    if do_vacuum:
        world.store.vacuum()
    events = int(world.store.conn.execute("SELECT COUNT(*) FROM events").fetchone()[0])
    return {
        "ok": True,
        "schema_version": world.store.schema_version(),
        "events": events,
        "event_retention": world.store.event_retention,
        "comms_pruned": comms_pruned,
        "comms_days": days,
        "vacuum": do_vacuum,
    }


# How many recent events to scan when aggregating incidents. Matches the
# window a human would review on the dashboard, not the full retention.
_RECENT_EVENT_WINDOW = 200
_MESSAGE_LIMIT_MAX = 200


def _tick_ring(world: "World") -> list[dict[str, Any]]:
    ring = world.store.get_kv(TICK_METRICS_KEY)
    if not isinstance(ring, list):
        return []
    return [entry for entry in ring if isinstance(entry, dict)]


def _recent_agent_errors(world: "World") -> dict[str, int]:
    counts: dict[str, int] = {}
    for event in world.store.events(_RECENT_EVENT_WINDOW):
        if event.get("kind") != "agent_error":
            continue
        agent = str(event.get("agent") or "unknown")
        counts[agent] = counts.get(agent, 0) + 1
    return counts


def metrics(world: "World") -> dict[str, Any]:
    """Read-only runtime snapshot: tick timings, comms, pipeline, revenue, agents.

    Pure read — safe to call from the dashboard on every poll; never writes.
    """
    ring = _tick_ring(world)
    ms_values = [float(entry.get("ms") or 0.0) for entry in ring]
    snap = world.ledger.snapshot(now=world.now)
    return {
        "tick": world.tick,
        "mode": world.config.mode,
        "ticks": {
            "recent": ring[-10:],
            "avg_ms": round(sum(ms_values) / len(ms_values), 2) if ms_values else 0,
            "last_ms": ms_values[-1] if ms_values else 0,
        },
        "comms": world.comms.counts() if world.comms is not None else {},
        "pipeline": world.store.job_counts(),
        "revenue": {
            "trailing_30d_usd": snap["trailing_30d_usd"],
            "lifetime_usd": snap["revenue_usd"],
            "equity_usd": snap["equity_usd"],
        },
        "agents": {
            "frozen": sorted(world.frozen),
            "reputation": dict(world.reputation.scores),
            "recent_errors": _recent_agent_errors(world),
        },
        "cognition": world.router.snapshot(),
    }


def write_weekly_report(world: "World") -> Path:
    """Render the ISO-week operations report to markdown and return its path.

    Idempotent per ISO week: rerunning within the same week overwrites the
    same ``artifacts/reports/week_<year>-W<week>.md`` file. Also records the
    data-dir-relative path under the ``last_weekly_report`` kv key.
    """
    paths = world.config.paths()
    reports_dir = paths.artifacts / "reports"
    reports_dir.mkdir(parents=True, exist_ok=True)
    iso_year, iso_week, _ = world.now.isocalendar()
    path = reports_dir / f"week_{iso_year}-W{iso_week:02d}.md"

    snap = world.ledger.snapshot(now=world.now)
    job_counts = world.store.job_counts()
    comms_counts = world.comms.counts() if world.comms is not None else {}
    agent_errors = sum(_recent_agent_errors(world).values())
    certified = [
        str(report.get("strategy_id") or "unnamed")
        for report in world.certified
        if report.get("certified")
    ]
    frozen = ", ".join(
        f"{agent} ({(world.freeze_info.get(agent) or {}).get('kind', 'unknown')})"
        for agent in sorted(world.frozen)
    )
    goals = world.config.goals
    trailing = float(snap["trailing_30d_usd"])

    def goal_line(label: str, target: float) -> str:
        pct = f"{100.0 * trailing / target:.1f}%" if target else "n/a"
        return f"- {label} ${target:,.0f}/mo: {pct} (trailing ${trailing:,.2f})"

    lines = [
        f"# Weekly report — {world.config.firm_name} — {iso_year}-W{iso_week:02d}",
        "",
        f"- Mode: {world.config.mode}",
        f"- Generated: {world.stamp()}",
        f"- Tick: {world.tick}",
        "",
        "## Revenue",
        "",
        f"- Trailing 30d: ${trailing:,.2f}",
        f"- Lifetime: ${float(snap['revenue_usd']):,.2f}",
        f"- Labor: ${float(snap['labor_usd']):,.2f}",
        f"- Products: ${float(snap['products_usd']):,.2f}",
        f"- Retainers: ${float(snap['retainers_usd']):,.2f}",
        "",
        "## Pipeline",
        "",
        "| Status | Jobs |",
        "| --- | --- |",
    ]
    if job_counts:
        lines.extend(f"| {status} | {count} |" for status, count in sorted(job_counts.items()))
    else:
        lines.append("| (none) | 0 |")
    lines += [
        "",
        "## Invoices",
        "",
        f"- Open: {len(world.store.invoices('open'))}",
        f"- Paid: {len(world.store.invoices('paid'))}",
        "",
        "## Comms health",
        "",
        "| Status | Messages |",
        "| --- | --- |",
    ]
    lines.extend(
        f"| {status} | {int(comms_counts.get(status, 0))} |"
        for status in (STATUS_QUEUED, STATUS_DONE, STATUS_EXPIRED, STATUS_DEAD)
    )
    lines += [
        "",
        "## Incidents",
        "",
        f"- Agent errors (last {_RECENT_EVENT_WINDOW} events): {agent_errors}",
        f"- Frozen agents: {frozen or 'none'}",
        "",
        "## Strategies",
        "",
        f"- Certified: {len(certified)} ({', '.join(certified) if certified else 'none'})",
        "",
        "## Goals progress",
        "",
        goal_line("Minimum", goals.minimum_usd),
        goal_line("Recommended", goals.recommended_usd),
        goal_line("Good", goals.good_usd),
        "",
    ]
    path.write_text("\n".join(lines))
    world.store.set_kv("last_weekly_report", path.relative_to(paths.root).as_posix())
    return path


def sanitized_messages(
    world: "World", status: str | None = None, limit: int = 50
) -> list[dict[str, Any]]:
    """Newest-first bus messages with routing metadata only — never payloads.

    Raises ``ValueError`` on an unknown status or a limit outside 1..200 so
    callers (the dashboard) can reject bad input before touching the store.
    """
    if status is not None and status not in STATUSES:
        raise ValueError(f"status must be one of: {', '.join(sorted(STATUSES))}")
    if not isinstance(limit, int) or isinstance(limit, bool) or not (
        1 <= limit <= _MESSAGE_LIMIT_MAX
    ):
        raise ValueError(f"limit must be an int in 1..{_MESSAGE_LIMIT_MAX}")
    rows = world.store.messages(status=status, limit=None)
    return [
        {
            "id": row["id"],
            "ts": row["ts"],
            "kind": row["kind"],
            "sender": row["sender"],
            "recipient": row["recipient"],
            "status": row["status"],
            "attempts": int(row.get("attempts") or 0),
            "error": row.get("error"),
        }
        for row in reversed(rows[-limit:])
    ]
