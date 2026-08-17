from __future__ import annotations

import json
import uuid
from typing import Any, TYPE_CHECKING

from sovereign.memory.store import iso

if TYPE_CHECKING:
    from sovereign.engine.world import World


def send(
    world: "World",
    to: str,
    subject: str,
    body: str,
    job_id: str | None = None,
    kind: str = "outbound",
) -> dict[str, Any]:
    msg = {
        "id": "mail_" + uuid.uuid4().hex[:10],
        "ts": world.stamp(),
        "direction": "out",
        "address": to,
        "subject": subject,
        "body": body,
        "job_id": job_id,
        "kind": kind,
        "status": "queued",
    }
    world.store.upsert_mail(msg)
    path = world.config.paths().mail_outbox / f"{msg['id']}.json"
    path.write_text(json.dumps(msg, indent=2))
    # SMTP if the human injected it; otherwise the outbox *is* the send in this engine
    smtp_host = world.wallet.get_credential("SMTP_HOST")
    if smtp_host:
        try:
            _smtp_send(world, msg)
            msg["status"] = "sent"
        except Exception as e:
            msg["status"] = "queued"
            msg["smtp_error"] = str(e)[:200]
    else:
        msg["status"] = "sent_local"
    world.store.upsert_mail(msg)
    sent = world.config.paths().mail_sent / f"{msg['id']}.json"
    sent.write_text(json.dumps(msg, indent=2))
    if path.exists() and msg["status"] != "queued":
        path.unlink()
    return msg


def ingest_dropins(world: "World") -> list[dict[str, Any]]:
    """Human or webhook can drop JSON files into data/mail/inbox/."""
    ingested = []
    inbox = world.config.paths().mail_inbox
    for p in sorted(inbox.glob("*.json")):
        try:
            raw = json.loads(p.read_text())
        except Exception:
            continue
        msg = {
            "id": raw.get("id") or "mail_" + uuid.uuid4().hex[:10],
            "ts": raw.get("ts") or world.stamp(),
            "direction": "in",
            "address": raw.get("from") or raw.get("address") or "",
            "subject": raw.get("subject") or "",
            "body": raw.get("body") or "",
            "job_id": raw.get("job_id"),
            "kind": "inbound",
            "status": "unread",
        }
        world.store.upsert_mail(msg)
        ingested.append(msg)
        p.rename(world.config.paths().mail_sent / f"in_{p.name}")
    return ingested


def interpret(msg: dict[str, Any]) -> dict[str, str] | None:
    blob = f"{msg.get('subject','')} {msg.get('body','')} {msg.get('job_id') or ''}".lower()
    job_id = msg.get("job_id") or _extract_job_id(blob)
    if not job_id:
        return None
    if _has_word(blob, "accepted", "go ahead", "you're hired", "you are hired") or _has_word(blob, "accept"):
        return {"job_id": job_id, "action": "accept"}
    if _has_word(blob, "rejected", "reject", "not a fit") or _has_word(blob, "pass"):
        return {"job_id": job_id, "action": "reject"}
    if _has_word(blob, "paid", "txid", "payment received"):
        return {"job_id": job_id, "action": "paid_claim"}
    return {"job_id": job_id, "action": "note"}


def _has_word(blob: str, *words: str) -> bool:
    import re

    for w in words:
        if re.search(rf"\b{re.escape(w)}\b", blob):
            return True
    return False


def _extract_job_id(blob: str) -> str | None:
    for part in blob.replace("/", " ").replace("[", " ").replace("]", " ").split():
        if part.startswith("job_") and len(part) >= 8:
            return part.strip(".,;:")
    return None


def _smtp_send(world: "World", msg: dict[str, Any]) -> None:
    import smtplib
    from email.message import EmailMessage

    host = world.wallet.get_credential("SMTP_HOST") or ""
    port = int(world.wallet.get_credential("SMTP_PORT") or "587")
    user = world.wallet.get_credential("SMTP_USER") or ""
    password = world.wallet.get_credential("SMTP_PASS") or ""
    frm = world.wallet.get_credential("SMTP_FROM") or user
    em = EmailMessage()
    em["From"] = frm
    em["To"] = msg["address"]
    em["Subject"] = msg["subject"]
    em.set_content(msg["body"])
    with smtplib.SMTP(host, port, timeout=20) as s:
        s.starttls()
        if user:
            s.login(user, password)
        s.send_message(em)
