from __future__ import annotations

from typing import TYPE_CHECKING, Any

from sovereign.security import validate_job_id

if TYPE_CHECKING:
    from sovereign.engine.world import World


ACCEPT_FROM = frozenset({"open", "applied", "proposed", "queued_budget", "needs_channel"})
ACCEPTED_OR_LATER = frozenset({"accepted", "in_progress", "delivered", "invoiced", "paid"})
REJECT_FROM = frozenset({"open", "applied", "proposed", "queued_budget", "needs_channel", "accepted"})
TERMINAL_OR_PROTECTED = frozenset(
    {"rejected", "expired", "cancelled", "in_progress", "delivered", "invoiced", "paid", "refunded", "void"}
)


def accept_job(world: World, job_id: str, source: str = "manual") -> dict[str, Any]:
    job_id = validate_job_id(job_id)
    job = world.store.get_job(job_id)
    if not job:
        raise KeyError(job_id)
    status = str(job.get("status") or "open").lower()
    if status in ACCEPTED_OR_LATER or status in TERMINAL_OR_PROTECTED:
        return job
    if status not in ACCEPT_FROM:
        raise ValueError(f"cannot accept job from status {status!r}")
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


def reject_job(world: World, job_id: str, source: str = "manual") -> dict[str, Any]:
    job_id = validate_job_id(job_id)
    job = world.store.get_job(job_id)
    if not job:
        raise KeyError(job_id)
    status = str(job.get("status") or "open").lower()
    if status in TERMINAL_OR_PROTECTED:
        return job
    if status not in REJECT_FROM:
        raise ValueError(f"cannot reject job from status {status!r}")
    job["status"] = "rejected"
    job["rejected_via"] = source
    world.store.upsert_job(job)
    world.store.outcome("proposal", 0, False, job.get("title", ""), "closer", "labor_studio")
    return job
