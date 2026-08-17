from __future__ import annotations

from typing import Any, TYPE_CHECKING

if TYPE_CHECKING:
    from sovereign.engine.world import World


def record(world: "World", skill_id: str, success: bool, usd: float = 0.0) -> dict[str, Any]:
    skills = dict(world.store.get_kv("skills") or {})
    s = dict(skills.get(skill_id) or {"n": 0, "wins": 0, "usd": 0.0})
    s["n"] = int(s["n"]) + 1
    if success:
        s["wins"] = int(s["wins"]) + 1
        s["usd"] = float(s["usd"]) + float(usd)
    skills[skill_id] = s
    world.store.set_kv("skills", skills)
    return s


def snapshot(world: "World") -> dict[str, Any]:
    return dict(world.store.get_kv("skills") or {})
