"""Built-in bus message handlers and the per-agent inbox pump.

A handler is ``(world, message) -> dict | None``; the dict is the reply
payload when the message expects a reply. Dispatch is gated by the
recipient's :data:`~sovereign.agents.spec.AgentSpec.handles`, so an agent can
only ever run handlers for kinds its spec grants. Replies (rows with
``reply_to`` set) are data addressed to the original requester: they are
acknowledged here and consumed via ``bus.replies(correlation_id)``, never
dispatched to a handler.

Summaries returned by :func:`process_inbox` carry ids, kinds, and fixed
status strings only — never message payload contents — so they are safe to
emit into the audit event stream.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any, Callable

from sovereign.agents.spec import spec_for
from sovereign.comms.bus import Message
from sovereign.governance.council import director_vote, risk_vote, treasurer_vote

if TYPE_CHECKING:
    from sovereign.engine.world import World

__all__ = ["HANDLERS", "Handler", "process_inbox"]

Handler = Callable[["World", Message], dict[str, Any] | None]

# Per-seat vote policies, pure functions of world state (see governance.council).
_VOTE_POLICIES: dict[str, Callable[["World", float], tuple[str, str]]] = {
    "treasurer": treasurer_vote,
    "risk": risk_vote,
    "director": lambda world, usd: director_vote(world),
}


def _ping(world: "World", message: Message) -> dict[str, Any]:
    return {"pong": True, "agent": message.recipient}


def _notify(world: "World", message: Message) -> None:
    return None


def _vote_request(world: "World", message: Message) -> dict[str, Any]:
    payload = message.payload
    action = payload.get("action")
    action_id = payload.get("action_id")
    usd = payload.get("usd")
    # Validation errors become message error text and can surface in
    # dead-letter events, so they name fields but never echo payload values.
    if not isinstance(action, str) or not action:
        raise ValueError("vote_request payload field 'action' must be a non-empty string")
    if not isinstance(action_id, str) or not action_id:
        raise ValueError("vote_request payload field 'action_id' must be a non-empty string")
    if isinstance(usd, bool) or not isinstance(usd, (int, float)):
        raise ValueError("vote_request payload field 'usd' must be a number")
    policy = _VOTE_POLICIES.get(message.recipient)
    if policy is None:
        raise ValueError(f"agent {message.recipient!r} holds no vote policy")
    vote, reason = policy(world, float(usd))
    return {"vote": vote, "reason": reason}


HANDLERS: dict[str, Handler] = {
    "ping": _ping,
    "notify": _notify,
    "vote_request": _vote_request,
}


def _resolve(agent: str, kind: str) -> Handler | None:
    try:
        handles = spec_for(agent).handles
    except KeyError:
        return None
    if kind not in handles:
        return None
    return HANDLERS.get(kind)


def process_inbox(world: "World", agent: str, *, max_messages: int = 10) -> list[dict[str, Any]]:
    """Drain up to ``max_messages`` queued deliveries for one agent.

    Per message: replies are acknowledged as received data; other kinds are
    dispatched to their handler (reply-then-ack on success), dead-lettered as
    ``unhandled_kind`` when the recipient's spec does not grant the kind, and
    failed (retry, then dead-letter at max_attempts) on handler exceptions.
    One bad message never breaks the loop.
    """
    bus = world.comms
    if bus is None:
        return []
    summaries: list[dict[str, Any]] = []
    for message in bus.inbox(agent, now=world.now, limit=max_messages):
        summary: dict[str, Any] = {
            "id": message.id,
            "kind": message.kind,
            "sender": message.sender,
        }
        try:
            if message.reply_to is not None:
                bus.ack(message.id, now=world.now)
                summary.update({"action": "reply_received", "status": "done"})
            else:
                handler = _resolve(agent, message.kind)
                if handler is None:
                    bus.dead_letter(message, "unhandled_kind", now=world.now)
                    summary.update(
                        {"action": "dead_letter", "status": "dead", "reason": "unhandled_kind"}
                    )
                else:
                    result = handler(world, message)
                    if message.expects_reply and result is not None:
                        bus.reply(message, agent, result, now=world.now)
                        summary["action"] = "replied"
                    else:
                        summary["action"] = "acked"
                    bus.ack(message.id, now=world.now)
                    summary["status"] = "done"
        except Exception as exc:  # noqa: BLE001 - one bad message must not stop the pump
            summary["action"] = "failed"
            try:
                summary["status"] = bus.fail(message, str(exc)[:200], now=world.now)
            except Exception:  # noqa: BLE001 - even a broken fail must not stop the pump
                summary["status"] = "error"
        summaries.append(summary)
    return summaries
