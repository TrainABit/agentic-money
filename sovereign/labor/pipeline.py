from __future__ import annotations

from typing import Any, TYPE_CHECKING

if TYPE_CHECKING:
    from sovereign.engine.world import World


def accept_job(world: "World", job_id: str, source: str = "manual") -> dict[str, Any]:
    job = world.store.get_job(job_id)
    if not job:
        raise KeyError(job_id)
    if job.get("status") in {"accepted", "in_progress", "delivered", "invoiced", "paid"}:
        return job
    job["status"] = "accepted"
    job["accepted_via"] = source
    variant = job.get("ab_variant")
    if variant in {"trial", "control"}:
        ab = dict(world.store.get_kv("ab_closer") or {})
        key = f"{variant}_usd"
        ab[key] = float(ab.get(key, 0)) + float(job.get("price_usd") or 0)
        ab[f"{variant}_wins"] = int(ab.get(f"{variant}_wins", 0)) + 1
        world.store.set_kv("ab_closer", ab)
    world.store.upsert_job(job)
    world.store.outcome("proposal", float(job.get("price_usd") or 0), True, job.get("title", ""), "closer", "labor_studio")
    world.reputation.boost("closer", 1.5, f"accepted via {source}")
    from sovereign.memory.skills import record

    record(world, "closer.accept", True, float(job.get("price_usd") or 0))
    return job


def reject_job(world: "World", job_id: str, source: str = "manual") -> dict[str, Any]:
    job = world.store.get_job(job_id)
    if not job:
        raise KeyError(job_id)
    job["status"] = "rejected"
    job["rejected_via"] = source
    world.store.upsert_job(job)
    world.store.outcome("proposal", 0, False, job.get("title", ""), "closer", "labor_studio")
    return job
