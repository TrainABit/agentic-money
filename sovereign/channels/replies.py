from __future__ import annotations

from typing import Any, TYPE_CHECKING

if TYPE_CHECKING:
    from sovereign.engine.world import World

SENSITIVE = {
    "EXCHANGE_API_SECRET",
    "EXCHANGE_API_KEY",
    "HETZNER_API_TOKEN",
    "SMTP_PASS",
    "STRIPE_SECRET",
}


def consume(world: "World") -> list[dict[str, Any]]:
    """Turn filled human replies into credentials / job state. Never emit secret values."""
    applied = []
    items = world.human.all()
    dirty = False
    for it in items:
        if it.get("status") != "filled" or it.get("consumed"):
            continue
        fields = dict(it.get("reply") or {})
        service = it.get("service") or ""
        flags = []
        for k, v in fields.items():
            if not v:
                continue
            if k in SENSITIVE or k.isupper():
                world.wallet.put_credential(k, v)
                flags.append(k)
            elif k.lower() in {"ok", "done"} and str(v) in {"1", "true", "yes", "ok"}:
                world.wallet.put_credential(f"{service}_ready", "1")
                flags.append(f"{service}_ready")
        job_id = fields.get("job_id") or fields.get("JOB_ID")
        status = (fields.get("status") or "").lower()
        if job_id and status in {"accepted", "accept"}:
            from sovereign.labor.pipeline import accept_job

            accept_job(world, job_id, source="human")
            flags.append("job_accept")
        if job_id and status in {"paid"}:
            from sovereign.capital.invoice import collect

            try:
                collect(world, job_id, source="human")
                flags.append("job_paid")
            except KeyError:
                pass
        it["consumed"] = True
        it["consumed_flags"] = flags
        it["reply"] = {k: "[vaulted]" for k in fields}
        dirty = True
        applied.append({"id": it["id"], "service": service, "flags": flags})
        world.store.emit("human_consumed", {"id": it["id"], "service": service, "flags": flags}, "courier")
    if dirty:
        world.human._write(items)
    return applied
