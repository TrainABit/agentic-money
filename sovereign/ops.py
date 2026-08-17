"""Operational readiness: one verdict on whether this engine can run.

:func:`readiness` runs local, side-effect-free probes (no network calls) and
returns a machine-readable report. Required checks gate the ``sovereign
bootstrap`` exit code; informational checks surface state a human should know
about (legacy wallets, certification coverage, comms backlog) without
blocking a healthy engine.
"""

from __future__ import annotations

import sys
from typing import Any, TYPE_CHECKING

from sovereign.agents.spec import roster, tool_matrix
from sovereign.comms.bus import Bus, STATUS_QUEUED
from sovereign.heal.checks import diagnose

if TYPE_CHECKING:
    from sovereign.engine.world import World

__all__ = ["readiness"]

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

    findings = diagnose(world)
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

    ready = all(check["ok"] for check in checks if check["required"])
    return {"ready": ready, "mode": world.config.mode, "checks": checks}
