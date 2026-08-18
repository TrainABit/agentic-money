"""Outbound/inbound mail transports.

AgentMail is the primary transport; SMTP and the local file outbox remain as
fallbacks. The selection order is credential-driven: vaulted AgentMail
credentials win, then SMTP, then the local outbox. All inbound remote mail is
untrusted data and only ever re-enters the engine through the hardened
drop-in pipeline (see sovereign.channels.mail.ingest_remote_inbound).
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from sovereign.engine.world import World

PROCESSED_LABEL = "sovereign-processed"
MAX_BODY_CHARS = 20_000
POLL_SCAN_CAP = 100


@dataclass
class OutboundResult:
    """Outcome of one outbound transport attempt."""

    status: str  # "sent" | "queued"
    transport: str = "local"
    error: str | None = None


class AgentMailTransport:
    """Thin wrapper over the AgentMail SDK client (injectable for tests)."""

    def __init__(self, api_key: str, inbox_id: str, client: Any | None = None) -> None:
        self.inbox_id = inbox_id
        if client is None:
            try:
                from agentmail import AgentMail
            except ImportError as exc:
                raise RuntimeError(
                    "the 'agentmail' package is not installed; "
                    "install the [mail] extra (pip install 'sovereign[mail]') "
                    "to enable the AgentMail transport"
                ) from exc
            client = AgentMail(api_key=api_key)
        self.client = client

    def send(self, to: str, subject: str, body: str) -> None:
        """Send one message. Exceptions bubble to the caller for queueing."""
        self.client.inboxes.messages.send(
            inbox_id=self.inbox_id,
            to=to,
            subject=subject,
            text=body,
        )

    def poll(self, limit: int = 25, seen_ids: set[str] | None = None) -> list[dict[str, Any]]:
        """Fetch unprocessed inbound messages and label them processed.

        Pages through inbox metadata (list returns no bodies), skips ids in
        ``seen_ids`` and messages already labeled PROCESSED_LABEL, fetches each
        remaining full message, and labels it after a successful build. A bad
        individual message is skipped, never fatal for the whole poll.
        """
        seen = seen_ids or set()
        results: list[dict[str, Any]] = []
        scanned = 0
        page_token: str | None = None
        while scanned < POLL_SCAN_CAP:
            page = self.client.inboxes.messages.list(
                inbox_id=self.inbox_id,
                limit=limit,
                page_token=page_token,
            )
            items = list(_field(page, "messages") or [])
            if not items:
                break
            for item in items:
                if scanned >= POLL_SCAN_CAP:
                    break
                scanned += 1
                message_id = str(_field(item, "message_id", "id") or "")
                if not message_id or message_id in seen:
                    continue
                if PROCESSED_LABEL in (_field(item, "labels") or []):
                    continue
                try:
                    full = self.client.inboxes.messages.get(
                        inbox_id=self.inbox_id,
                        message_id=message_id,
                    )
                    entry = {
                        "agentmail_id": message_id,
                        "from": str(_field(full, "from_", "from") or ""),
                        "subject": str(_field(full, "subject") or ""),
                        "body": _body_of(full),
                        "ts": _timestamp_of(full),
                    }
                    self.client.inboxes.messages.update(
                        inbox_id=self.inbox_id,
                        message_id=message_id,
                        add_labels=[PROCESSED_LABEL],
                    )
                except Exception:  # noqa: BLE001 - one bad message must not stop the poll
                    continue
                results.append(entry)
            page_token = _field(page, "next_page_token")
            if not page_token:
                break
        return results


def agentmail_credentials(world: World) -> tuple[str, str] | None:
    api_key = world.wallet.get_credential("AGENTMAIL_API_KEY")
    inbox_id = world.wallet.get_credential("AGENTMAIL_INBOX_ID")
    if api_key and inbox_id:
        return api_key, inbox_id
    return None


def build_agentmail_transport(world: World) -> AgentMailTransport:
    """Factory for the live transport; tests inject ``world.agentmail_transport``."""
    override = getattr(world, "agentmail_transport", None)
    if override is not None:
        return override
    creds = agentmail_credentials(world)
    if creds is None:
        raise RuntimeError("AgentMail credentials are not configured")
    return AgentMailTransport(api_key=creds[0], inbox_id=creds[1])


def resolve_outbound_transport(
    world: World,
) -> tuple[str, Callable[[str, str, str], None] | None]:
    """Pick the outbound channel: agentmail, then smtp, then the local outbox."""
    if agentmail_credentials(world) is not None:

        def _send(to: str, subject: str, body: str) -> None:
            # Construct lazily so a missing SDK surfaces as a queueable send
            # failure instead of crashing the caller before the attempt.
            build_agentmail_transport(world).send(to, subject, body)

        return "agentmail", _send
    if world.wallet.get_credential("SMTP_HOST"):
        return "smtp", None
    return "local", None


def _field(obj: Any, *names: str) -> Any:
    for name in names:
        if isinstance(obj, dict):
            if obj.get(name) is not None:
                return obj[name]
        else:
            value = getattr(obj, name, None)
            if value is not None:
                return value
    return None


def _body_of(full: Any) -> str:
    body = (
        _field(full, "extracted_text")
        or _field(full, "text")
        or _field(full, "extracted_html")
        or _field(full, "html")
        or ""
    )
    return str(body)[:MAX_BODY_CHARS]


def _timestamp_of(full: Any) -> str:
    value = _field(full, "timestamp", "ts")
    if isinstance(value, datetime):
        return value.isoformat()
    return str(value) if value else ""
