from __future__ import annotations

import json
from typing import Any, TYPE_CHECKING

from sovereign.heal.checks import Finding, diagnose
from sovereign.memory.playbooks import seed_playbooks

if TYPE_CHECKING:
    from sovereign.engine.world import World


def setup(world: "World", full: bool = False) -> dict[str, Any]:
    """Idempotent repair. Safe to run every tick (cheap) or as `sovereign setup` (full)."""
    paths = world.config.paths()
    if full:
        paths.ensure()
        seed_playbooks(paths.playbooks)
        if world.tools is None:
            apply_repair(world, Finding("tools", False, "unbound", True, "bind_tools"))
        if not any(c.get("certified") for c in world.certified):
            try:
                apply_repair(world, Finding("strategies", False, "full setup", True, "recertify"))
            except Exception:
                pass
    findings = diagnose(world, deep=full)
    repairs: list[dict[str, Any]] = []
    for f in findings:
        if f.ok or not f.repairable or not f.repair:
            continue
        if f.repair == "recertify" and not full and world.tick % 10 != 0 and world.tick != 0:
            continue
        try:
            apply_repair(world, f)
            repairs.append({"code": f.code, "repair": f.repair, "ok": True})
        except Exception as e:
            repairs.append({"code": f.code, "repair": f.repair, "ok": False, "error": str(e)[:200]})
    after = diagnose(world, deep=full)
    report = {
        "healthy": all(x.ok for x in after),
        "findings": [f.as_dict() for f in after],
        "repairs": repairs,
        "full": full,
        "tick": world.tick,
    }
    world.store.set_kv("health", report)
    path = world.config.paths().artifacts / "health.json"
    path.write_text(json.dumps(report, indent=2, default=str))
    return report


def apply_repair(world: "World", f: Finding) -> None:
    paths = world.config.paths()
    if f.repair == "ensure_paths":
        paths.ensure()
    elif f.repair == "migrate":
        world.store._migrate()
    elif f.repair == "seed_playbooks":
        seed_playbooks(paths.playbooks)
    elif f.repair == "human_inbox":
        if not paths.human.exists():
            paths.human.write_text("[]")
        else:
            try:
                json.loads(paths.human.read_text())
            except Exception:
                bak = paths.human.with_suffix(".json.bak")
                bak.write_text(paths.human.read_text(errors="ignore"))
                paths.human.write_text("[]")
    elif f.repair == "stale_lock":
        if paths.lock.exists():
            paths.lock.unlink()
    elif f.repair == "wallet":
        world.wallet.load_or_create()
    elif f.repair == "recertify":
        from sovereign.engine.world import ensure_certified

        world.certified = []
        ensure_certified(world)
    elif f.repair == "sync_broker":
        book = world.treasury.trading_book()
        if world.broker.cash <= 0 and book > 0:
            world.broker.cash = book
    elif f.repair == "bind_tools":
        from sovereign.tools.catalog import build_registry

        world.tools = build_registry()
        world.tools.bind(world)
    else:
        raise ValueError(f"unknown repair {f.repair}")


def thaw_cooled(world: "World", cooldown: int = 5) -> list[str]:
    """Boost and thaw agents who sat in freeze long enough without new slashes to <20."""
    thawed = []
    for agent in list(world.frozen):
        since = int(world.freeze_since.get(agent, world.tick))
        if world.tick - since < cooldown:
            continue
        world.reputation.boost(agent, 12, "mechanic cooldown")
        if not world.reputation.should_freeze(agent):
            world.thaw(agent, "cooldown")
            thawed.append(agent)
        else:
            world.freeze_since[agent] = world.tick
    return thawed
