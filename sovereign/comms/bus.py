from __future__ import annotations

import json
import re
import uuid
from dataclasses import dataclass
from datetime import datetime
from typing import Any, Sequence

from sovereign.engine.schedule import aware_utc, parse_datetime
from sovereign.memory.store import Store, iso

STATUS_QUEUED = "queued"
STATUS_DONE = "done"
STATUS_EXPIRED = "expired"
STATUS_DEAD = "dead"
STATUSES = frozenset({STATUS_QUEUED, STATUS_DONE, STATUS_EXPIRED, STATUS_DEAD})

KIND_RE = re.compile(r"^[a-z][a-z0-9_.]{0,63}$")
KIND_MAX_LEN = 64
REPLY_SUFFIX = ".reply"
MAX_PAYLOAD_BYTES = 32_768
MAX_ATTEMPTS_CEILING = 10


def _new_id(prefix: str) -> str:
    return prefix + uuid.uuid4().hex


def _require_now(now: datetime) -> datetime:
    if not isinstance(now, datetime) or now.tzinfo is None:
        raise ValueError("now must be a timezone-aware datetime")
    return aware_utc(now)


def _default_reply_kind(kind: str) -> str:
    candidate = kind + REPLY_SUFFIX
    if len(candidate) > KIND_MAX_LEN:
        candidate = kind[: KIND_MAX_LEN - len(REPLY_SUFFIX)].rstrip("._") + REPLY_SUFFIX
    if not KIND_RE.match(candidate):
        raise ValueError(f"cannot derive a reply kind from {kind!r}")
    return candidate


@dataclass(frozen=True)
class Message:
    """One persisted point-to-point delivery from the messages table."""

    id: str
    ts: datetime
    thread_id: str
    correlation_id: str
    sender: str
    recipient: str
    kind: str
    payload: dict[str, Any]
    status: str
    expects_reply: bool
    reply_to: str | None
    deadline: datetime | None
    attempts: int
    max_attempts: int
    error: str | None

    @classmethod
    def from_record(cls, record: dict[str, Any]) -> "Message":
        ts = parse_datetime(record.get("ts"))
        if ts is None:
            raise ValueError(f"message {record.get('id')!r} has an invalid ts")
        payload = record.get("payload")
        if not isinstance(payload, dict):
            raise ValueError(f"message {record.get('id')!r} payload must be a dict")
        return cls(
            id=str(record["id"]),
            ts=ts,
            thread_id=str(record["thread_id"]),
            correlation_id=str(record["correlation_id"]),
            sender=str(record["sender"]),
            recipient=str(record["recipient"]),
            kind=str(record["kind"]),
            payload=dict(payload),
            status=str(record["status"]),
            expects_reply=bool(record.get("expects_reply")),
            reply_to=record.get("reply_to"),
            deadline=parse_datetime(record.get("deadline")),
            attempts=int(record.get("attempts") or 0),
            max_attempts=int(record.get("max_attempts") or 3),
            error=record.get("error"),
        )

    def to_record(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "ts": iso(self.ts),
            "thread_id": self.thread_id,
            "correlation_id": self.correlation_id,
            "sender": self.sender,
            "recipient": self.recipient,
            "kind": self.kind,
            "payload": dict(self.payload),
            "status": self.status,
            "expects_reply": 1 if self.expects_reply else 0,
            "reply_to": self.reply_to,
            "deadline": iso(self.deadline) if self.deadline is not None else None,
            "attempts": self.attempts,
            "max_attempts": self.max_attempts,
            "error": self.error,
        }


@dataclass(frozen=True)
class SendReceipt:
    thread_id: str
    correlation_id: str
    message_ids: tuple[str, ...]


class Bus:
    """Durable, auditable agent-to-agent messaging on top of the Store.

    Every row is a single (sender, recipient) delivery; multicasts share one
    thread/correlation id so fan-outs can be joined back together. All
    time-dependent entry points take an explicit timezone-aware ``now`` so
    simulated and live clocks behave identically; the bus never reads the
    wall clock. Events emitted for the audit trail carry ids and kinds only,
    never payload contents.
    """

    def __init__(self, store: Store, roster: frozenset[str]) -> None:
        self.store = store
        self.roster = frozenset(roster)
        if not self.roster:
            raise ValueError("roster must not be empty")

    def _require_agent(self, name: str, role: str) -> str:
        if name not in self.roster:
            raise ValueError(f"{role} {name!r} is not in the roster")
        return name

    def _normalize_recipients(self, recipients: str | Sequence[str]) -> tuple[str, ...]:
        candidates = (recipients,) if isinstance(recipients, str) else tuple(recipients)
        deduped = tuple(dict.fromkeys(candidates))
        if not deduped:
            raise ValueError("at least one recipient is required")
        for recipient in deduped:
            self._require_agent(recipient, "recipient")
        return deduped

    @staticmethod
    def _validate_kind(kind: str) -> str:
        if not isinstance(kind, str) or not KIND_RE.match(kind):
            raise ValueError(f"invalid message kind: {kind!r}")
        return kind

    @staticmethod
    def _validate_payload(payload: dict[str, Any]) -> dict[str, Any]:
        if not isinstance(payload, dict):
            raise ValueError("payload must be a dict")
        try:
            encoded = json.dumps(payload)
        except (TypeError, ValueError) as exc:
            raise ValueError("payload must be JSON-serializable") from exc
        size = len(encoded.encode("utf-8"))
        if size > MAX_PAYLOAD_BYTES:
            raise ValueError(f"payload is {size} bytes; limit is {MAX_PAYLOAD_BYTES}")
        return payload

    def send(
        self,
        sender: str,
        recipients: str | Sequence[str],
        kind: str,
        payload: dict[str, Any],
        *,
        now: datetime,
        thread_id: str | None = None,
        correlation_id: str | None = None,
        reply_to: str | None = None,
        expects_reply: bool = False,
        deadline: datetime | None = None,
        max_attempts: int = 3,
    ) -> SendReceipt:
        now = _require_now(now)
        self._require_agent(sender, "sender")
        targets = self._normalize_recipients(recipients)
        self._validate_kind(kind)
        self._validate_payload(payload)
        if deadline is not None:
            if not isinstance(deadline, datetime) or deadline.tzinfo is None:
                raise ValueError("deadline must be a timezone-aware datetime")
            deadline = aware_utc(deadline)
            if deadline <= now:
                raise ValueError("deadline must be after now")
        if not isinstance(max_attempts, int) or isinstance(max_attempts, bool) or not (
            1 <= max_attempts <= MAX_ATTEMPTS_CEILING
        ):
            raise ValueError(f"max_attempts must be an int in 1..{MAX_ATTEMPTS_CEILING}")

        thread = thread_id or _new_id("th_")
        correlation = correlation_id or _new_id("co_")
        ts = iso(now)
        deadline_ts = iso(deadline) if deadline is not None else None
        records = [
            {
                "id": _new_id("msg_"),
                "ts": ts,
                "thread_id": thread,
                "correlation_id": correlation,
                "sender": sender,
                "recipient": recipient,
                "kind": kind,
                "payload": payload,
                "status": STATUS_QUEUED,
                "expects_reply": 1 if expects_reply else 0,
                "reply_to": reply_to,
                "deadline": deadline_ts,
                "attempts": 0,
                "max_attempts": max_attempts,
                "error": None,
            }
            for recipient in targets
        ]
        with self.store.transaction():
            for record in records:
                self.store.insert_message(record)
        return SendReceipt(thread, correlation, tuple(r["id"] for r in records))

    def broadcast(
        self, sender: str, kind: str, payload: dict[str, Any], *, now: datetime, **kw: Any
    ) -> SendReceipt:
        self._require_agent(sender, "sender")
        peers = sorted(self.roster - {sender})
        return self.send(sender, peers, kind, payload, now=now, **kw)

    def request(
        self,
        sender: str,
        recipients: str | Sequence[str],
        kind: str,
        payload: dict[str, Any],
        *,
        now: datetime,
        deadline: datetime,
        **kw: Any,
    ) -> SendReceipt:
        if deadline is None:
            raise ValueError("request deadline is required")
        return self.send(
            sender,
            recipients,
            kind,
            payload,
            now=now,
            deadline=deadline,
            expects_reply=True,
            **kw,
        )

    def reply(
        self,
        original: Message,
        sender: str,
        payload: dict[str, Any],
        *,
        now: datetime,
        kind: str | None = None,
    ) -> SendReceipt:
        if original.reply_to is not None:
            raise ValueError("cannot reply to a reply")
        if sender != original.recipient:
            raise ValueError(
                f"reply sender must be {original.recipient!r}, the original recipient"
            )
        reply_kind = kind if kind is not None else _default_reply_kind(original.kind)
        return self.send(
            sender,
            original.sender,
            reply_kind,
            payload,
            now=now,
            thread_id=original.thread_id,
            correlation_id=original.correlation_id,
            reply_to=original.id,
            expects_reply=False,
        )

    def inbox(self, agent: str, *, now: datetime, limit: int = 20) -> list[Message]:
        """Queued deliveries for one agent, oldest first, hiding past-deadline rows.

        Rows past their deadline stay hidden even before an expire_due sweep
        marks them, so may return fewer than ``limit`` messages.
        """
        now = _require_now(now)
        self._require_agent(agent, "agent")
        visible: list[Message] = []
        for row in self.store.messages(recipient=agent, status=STATUS_QUEUED, limit=limit):
            message = Message.from_record(row)
            if message.deadline is not None and message.deadline < now:
                continue
            visible.append(message)
        return visible

    def ack(self, message_id: str, *, now: datetime) -> Message:
        _require_now(now)
        with self.store.transaction():
            record = self.store.get_message(message_id)
            if record is None:
                raise KeyError(message_id)
            if record["status"] != STATUS_QUEUED:
                raise ValueError(f"cannot ack message in status {record['status']!r}")
            record["status"] = STATUS_DONE
            self.store.update_message(record)
        return Message.from_record(record)

    def fail(self, message: Message, error: str, *, now: datetime) -> str:
        _require_now(now)
        reason = str(error)
        with self.store.transaction():
            record = self.store.get_message(message.id)
            if record is None:
                raise KeyError(message.id)
            if record["status"] != STATUS_QUEUED:
                raise ValueError(f"cannot fail message in status {record['status']!r}")
            record["attempts"] = int(record["attempts"]) + 1
            record["error"] = reason
            if record["attempts"] >= int(record["max_attempts"]):
                record["status"] = STATUS_DEAD
            self.store.update_message(record)
            if record["status"] == STATUS_DEAD:
                self._emit_dead_letter(record, reason)
        return str(record["status"])

    def dead_letter(self, message: Message, reason: str, *, now: datetime) -> str:
        _require_now(now)
        text = str(reason)
        with self.store.transaction():
            record = self.store.get_message(message.id)
            if record is None:
                raise KeyError(message.id)
            if record["status"] != STATUS_QUEUED:
                raise ValueError(f"cannot dead-letter message in status {record['status']!r}")
            record["status"] = STATUS_DEAD
            record["error"] = text
            self.store.update_message(record)
            self._emit_dead_letter(record, text)
        return STATUS_DEAD

    def _emit_dead_letter(self, record: dict[str, Any], error: str) -> None:
        self.store.emit(
            "comms_dead_letter",
            {
                "id": record["id"],
                "kind": record["kind"],
                "recipient": record["recipient"],
                "error": error,
            },
            agent=record["recipient"],
        )

    def replies(self, correlation_id: str) -> list[Message]:
        rows = self.store.messages(correlation_id=correlation_id, limit=None)
        return [Message.from_record(row) for row in rows if row.get("reply_to")]

    def outstanding(self, correlation_id: str) -> int:
        """Queued request rows still awaiting an ack for this correlation."""
        rows = self.store.messages(
            correlation_id=correlation_id, status=STATUS_QUEUED, limit=None
        )
        return sum(1 for row in rows if row.get("expects_reply"))

    def expire_due(self, *, now: datetime) -> int:
        now = _require_now(now)
        expired = 0
        with self.store.transaction():
            for row in self.store.messages(status=STATUS_QUEUED, limit=None):
                deadline = parse_datetime(row.get("deadline"))
                if deadline is None or deadline >= now:
                    continue
                row["status"] = STATUS_EXPIRED
                self.store.update_message(row)
                expired += 1
            if expired:
                self.store.emit("comms_expired", {"count": expired})
        return expired

    def counts(self) -> dict[str, int]:
        return self.store.message_counts()
