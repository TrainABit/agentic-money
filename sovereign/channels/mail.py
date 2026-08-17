from __future__ import annotations

import hashlib
import hmac
import json
import re
import uuid
from email.utils import parseaddr
from typing import TYPE_CHECKING, Any

from sovereign.fileio import atomic_write_text, file_lock
from sovereign.security import validate_job_id

if TYPE_CHECKING:
    from sovereign.engine.world import World


_ADDRESS_RE = re.compile(
    r"\A(?P<local>[A-Za-z0-9!#$%&'*+/=?^_`{|}~-]+"
    r"(?:\.[A-Za-z0-9!#$%&'*+/=?^_`{|}~-]+)*)@"
    r"(?P<domain>[A-Za-z0-9](?:[A-Za-z0-9-]{0,61}[A-Za-z0-9])?"
    r"(?:\.[A-Za-z0-9](?:[A-Za-z0-9-]{0,61}[A-Za-z0-9])?)+)\Z"
)


def validate_recipient(value: object) -> str:
    return _parse_address(value, reject_noreply=True)


def _parse_address(value: object, *, reject_noreply: bool) -> str:
    if not isinstance(value, str) or not value or len(value) > 320:
        raise ValueError("invalid email recipient")
    if any(ch in value for ch in "\r\n\x00") or "," in value or ";" in value:
        raise ValueError("invalid email recipient")
    source = value.strip()
    _display, address = parseaddr(source)
    match = _ADDRESS_RE.fullmatch(address)
    if not match:
        raise ValueError("invalid email recipient")
    if source != address:
        display_form = re.fullmatch(r"[^<>]{1,100}<\s*" + re.escape(address) + r"\s*>", source)
        if not display_form:
            raise ValueError("invalid email recipient")
    local = match.group("local")
    domain = match.group("domain").lower()
    if len(local) > 64 or len(address) > 254:
        raise ValueError("invalid email recipient")
    mailbox = re.sub(r"[._-]", "", local.split("+", 1)[0].lower())
    if reject_noreply and (
        mailbox.startswith(("noreply", "donotreply")) or mailbox == "mailerdaemon"
    ):
        raise ValueError("noreply recipients are not allowed")
    return f"{local}@{domain}"


def _validated_subject(value: object) -> str:
    if not isinstance(value, str) or any(ch in value for ch in "\r\n\x00"):
        raise ValueError("invalid email subject")
    return value


def send(
    world: World,
    to: str,
    subject: str,
    body: str,
    job_id: str | None = None,
    kind: str = "outbound",
) -> dict[str, Any]:
    recipient = validate_recipient(to)
    clean_subject = _validated_subject(subject)
    clean_job_id = validate_job_id(job_id) if job_id is not None else None
    if not isinstance(kind, str) or not kind or len(kind) > 80 or any(ch in kind for ch in "\r\n\x00"):
        raise ValueError("invalid mail kind")
    if not isinstance(body, str):
        raise TypeError("invalid email body")

    paths = world.config.paths()
    lock_path = paths.mail_outbox / ".send.lock"
    with file_lock(lock_path):
        if clean_job_id is not None:
            for existing in world.store.mail(direction="out"):
                if existing.get("job_id") == clean_job_id and existing.get("kind") == kind:
                    return existing
            digest = hashlib.sha256(f"{clean_job_id}\x00{kind}".encode()).hexdigest()[:16]
            message_id = "mail_" + digest
        else:
            message_id = "mail_" + uuid.uuid4().hex[:10]
        msg = {
            "id": message_id,
            "ts": world.stamp(),
            "direction": "out",
            "address": recipient,
            "subject": clean_subject,
            "body": body,
            "job_id": clean_job_id,
            "kind": kind,
            "status": "queued",
        }
        # Persist the idempotency record before attempting the irreversible send.
        world.store.upsert_mail(msg)
        path = paths.mail_outbox / f"{msg['id']}.json"
        atomic_write_text(path, json.dumps(msg, indent=2), mode=0o600)
        smtp_host = world.wallet.get_credential("SMTP_HOST")
        if smtp_host:
            try:
                _smtp_send(world, msg)
                msg["status"] = "sent"
            except Exception as exc:  # noqa: BLE001 - any provider failure must queue
                msg["status"] = "queued"
                msg["smtp_error"] = str(exc)[:200]
        else:
            msg["status"] = "sent_local"
        world.store.upsert_mail(msg)
        sent = paths.mail_sent / f"{msg['id']}.json"
        atomic_write_text(sent, json.dumps(msg, indent=2), mode=0o600)
        if path.exists() and msg["status"] != "queued":
            path.unlink()
        return msg


def ingest_dropins(world: World) -> list[dict[str, Any]]:
    """Human or webhook can drop JSON files into data/mail/inbox/."""
    ingested = []
    inbox = world.config.paths().mail_inbox
    for p in sorted(inbox.glob("*.json")):
        try:
            if p.is_symlink() or p.stat().st_size > 1024 * 1024:
                continue
            raw = json.loads(p.read_text())
        except (OSError, UnicodeDecodeError, json.JSONDecodeError):
            continue
        if not isinstance(raw, dict):
            continue
        msg = {
            "id": "mail_" + uuid.uuid4().hex[:10],
            "ts": str(raw.get("ts") or world.stamp()),
            "direction": "in",
            "address": str(raw.get("from") or raw.get("address") or ""),
            "subject": str(raw.get("subject") or ""),
            "body": str(raw.get("body") or ""),
            "job_id": raw.get("job_id"),
            "kind": "inbound",
            "status": "unread",
        }
        msg["state_change_authorized"] = authorize_state_change(
            world,
            msg,
            signature=_signature_from(raw),
        )
        world.store.upsert_mail(msg)
        ingested.append(msg)
        destination = world.config.paths().mail_sent / f"in_{p.name}"
        if destination.exists():
            destination = world.config.paths().mail_sent / f"in_{uuid.uuid4().hex[:8]}_{p.name}"
        p.rename(destination)
    return ingested


def interpret(msg: dict[str, Any]) -> dict[str, str] | None:
    blob = f"{msg.get('subject', '')} {msg.get('body', '')}".lower()
    job_id = _message_job_id(msg)
    if not job_id:
        return None
    actions = []
    if _has_affirmed_phrase(blob, "accepted", "accept", "go ahead", "you're hired", "you are hired"):
        actions.append("accept")
    if _has_affirmed_phrase(blob, "rejected", "reject", "not a fit", "pass"):
        actions.append("reject")
    if _has_affirmed_phrase(blob, "paid", "txid", "payment received"):
        actions.append("paid_claim")
    if len(actions) != 1:
        return {"job_id": job_id, "action": "note"}
    action = actions[0]
    if action in {"accept", "reject"} and msg.get("state_change_authorized") is not True:
        return {"job_id": job_id, "action": "note"}
    return {"job_id": job_id, "action": action}


def state_change_hmac(secret: str, msg: dict[str, Any]) -> str:
    """Create the signature accepted for a state-changing mail drop-in."""
    job_id = _message_job_id(msg) or ""
    try:
        sender = _parse_address(msg.get("address") or msg.get("from") or "", reject_noreply=False).lower()
    except ValueError:
        sender = ""
    canonical = json.dumps(
        [
            job_id,
            sender,
            str(msg.get("subject") or ""),
            str(msg.get("body") or ""),
        ],
        ensure_ascii=False,
        separators=(",", ":"),
    ).encode()
    return hmac.new(secret.encode(), canonical, hashlib.sha256).hexdigest()


def authorize_state_change(
    world: World,
    msg: dict[str, Any],
    *,
    signature: str | None = None,
) -> bool:
    """Authorize an accept/reject intent by contact, trust list, or HMAC."""
    job_id = _message_job_id(msg)
    if not job_id:
        return False
    job = world.store.get_job(job_id)
    if not job:
        return False
    try:
        sender = _parse_address(msg.get("address") or msg.get("from") or "", reject_noreply=False).lower()
    except ValueError:
        return False

    for field in ("contact", "email"):
        if not job.get(field):
            continue
        try:
            if hmac.compare_digest(
                sender,
                _parse_address(str(job[field]), reject_noreply=False).lower(),
            ):
                return True
        except ValueError:
            continue

    trusted = world.wallet.get_credential("MAIL_TRUSTED_SENDERS") or ""
    for candidate in re.split(r"[\s,]+", trusted):
        if not candidate:
            continue
        try:
            if hmac.compare_digest(sender, _parse_address(candidate, reject_noreply=False).lower()):
                return True
        except ValueError:
            continue

    supplied = signature or _signature_from(msg)
    secret = (
        world.wallet.get_credential("MAIL_HMAC_SECRET")
        or world.wallet.get_credential("SOVEREIGN_MAIL_HMAC_SECRET")
        or world.wallet.get_credential("WEBHOOK_HMAC_SECRET")
    )
    if supplied and secret:
        supplied = supplied.removeprefix("sha256=").strip().lower()
        expected = state_change_hmac(secret, msg)
        if len(supplied) == len(expected) and hmac.compare_digest(supplied, expected):
            return True

    # Simulated marketplace drop-ins are generated inside the closed-loop simulator.
    return world.config.mode == "sim" and job.get("source") == "sim-market"


def _signature_from(msg: dict[str, Any]) -> str | None:
    for key in ("hmac", "signature", "x_sovereign_signature", "x-sovereign-signature"):
        value = msg.get(key)
        if isinstance(value, str) and value:
            return value
    headers = msg.get("headers")
    if isinstance(headers, dict):
        for key, value in headers.items():
            if str(key).lower() in {"x-sovereign-signature", "x-signature"} and isinstance(value, str):
                return value
    return None


def _message_job_id(msg: dict[str, Any]) -> str | None:
    explicit = msg.get("job_id")
    if explicit is not None:
        try:
            return validate_job_id(explicit)
        except ValueError:
            return None
    blob = f"{msg.get('subject', '')} {msg.get('body', '')}".lower()
    return _extract_job_id(blob)


def _has_affirmed_phrase(blob: str, *phrases: str) -> bool:
    for phrase in phrases:
        pattern = r"(?<!\w)" + re.escape(phrase).replace(r"\ ", r"\s+") + r"(?!\w)"
        for match in re.finditer(pattern, blob):
            clause = re.split(r"[.!?;:\n]", blob[: match.start()])[-1][-80:]
            negators = re.findall(r"[a-z]+n't|[a-z]+", clause)
            if any(
                word
                in {
                    "not",
                    "no",
                    "never",
                    "without",
                    "cannot",
                    "can't",
                    "don't",
                    "doesn't",
                    "didn't",
                    "haven't",
                    "hasn't",
                    "hadn't",
                    "isn't",
                    "wasn't",
                    "weren't",
                    "won't",
                    "wouldn't",
                    "shouldn't",
                    "couldn't",
                }
                for word in negators[-8:]
            ):
                continue
            return True
    return False


def _extract_job_id(blob: str) -> str | None:
    for match in re.finditer(r"job_[^\s\[\](){}<>]+", blob.lower()):
        candidate = match.group().strip(".,;:!?")
        try:
            return validate_job_id(candidate)
        except ValueError:
            continue
    return None


def _smtp_send(world: World, msg: dict[str, Any]) -> None:
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
