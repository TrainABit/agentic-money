from __future__ import annotations

from typing import TYPE_CHECKING, Any

from sovereign.security import validate_job_id

if TYPE_CHECKING:
    from sovereign.engine.world import World

SENSITIVE = {
    "EXCHANGE_API_SECRET",
    "EXCHANGE_API_KEY",
    "HETZNER_API_TOKEN",
    "SMTP_PASS",
    "STRIPE_SECRET",
}
NON_CREDENTIAL_FIELDS = {"ok", "done", "note", "job_id", "status"}


def consume(world: World) -> list[dict[str, Any]]:
    """Turn filled human replies into credentials / job state. Never emit secret values."""
    def apply(items: list[dict[str, Any]]) -> list[dict[str, Any]]:
        applied = []
        for item in items:
            if item.get("status") != "filled" or item.get("consumed"):
                continue
            fields = dict(item.get("reply") or {})
            service = item.get("service") or ""
            flags = []
            requested_fields = {str(key) for key in (item.get("fields") or [])}
            vaulted_fields: set[str] = set()
            for key, value in fields.items():
                if not value:
                    continue
                explicitly_requested_credential = (
                    key in requested_fields and key.lower() not in NON_CREDENTIAL_FIELDS
                )
                if key in SENSITIVE or explicitly_requested_credential:
                    world.wallet.put_credential(key, value)
                    flags.append(key)
                    vaulted_fields.add(key)
                elif (
                    key in requested_fields
                    and key.lower() in {"ok", "done"}
                    and str(value).lower() in {"1", "true", "yes", "ok"}
                ):
                    world.wallet.put_credential(f"{service}_ready", "1")
                    flags.append(f"{service}_ready")
            job_id = fields.get("job_id") or fields.get("JOB_ID")
            status = str(fields.get("status") or "").lower()
            if job_id and status in {"accepted", "accept"}:
                from sovereign.labor.pipeline import accept_job

                try:
                    accept_job(world, job_id, source="human")
                    flags.append("job_accept")
                except (KeyError, ValueError):
                    pass
            if job_id and status == "paid":
                try:
                    clean_job_id = validate_job_id(job_id)
                except ValueError:
                    pass
                else:
                    flags.append("job_paid_claim")
                    world.store.emit(
                        "human_paid_claim",
                        {
                            "job_id": clean_job_id,
                            "settled": False,
                            "verification_required": True,
                        },
                        "courier",
                    )
            item["consumed"] = True
            item["consumed_flags"] = flags
            item["reply"] = {
                key: (
                    "[vaulted]"
                    if key in vaulted_fields
                    else "[consumed]"
                    if key.lower() in NON_CREDENTIAL_FIELDS
                    else "[ignored]"
                )
                for key in fields
            }
            applied.append({"id": item["id"], "service": service, "flags": flags})
            world.store.emit(
                "human_consumed",
                {"id": item["id"], "service": service, "flags": flags},
                "courier",
            )
        return applied

    return world.human.update(apply)
