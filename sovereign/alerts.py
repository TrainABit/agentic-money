"""Out-of-band incident alerting.

``detect`` derives P0/P1 incidents (invariant breaches, dead-letter storms,
trading halts, repeated agent freezes, unattributed payments) from current
world state as pure reads. ``AlertManager`` filters them by configured
severity, throttles per incident kind, and pushes the survivors through the
configured channel — operator email via ``sovereign.channels.mail`` or an
HTTP webhook — so an operator hears about a breach without watching the
dashboard. Delivery is best-effort by design: ``dispatch`` never raises, and
every attempt leaves an ``alert_sent`` or ``alert_error`` event (never
secrets, never webhook URLs) in the audit trail.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from datetime import timedelta
from typing import TYPE_CHECKING, Any

import httpx

from sovereign.channels import mail as mailbox
from sovereign.engine.schedule import aware_utc, parse_datetime

if TYPE_CHECKING:
    from sovereign.config import AlertConfig
    from sovereign.engine.world import World

__all__ = ["SEVERITY_ORDER", "Alert", "AlertManager", "detect"]

SEVERITY_ORDER = {"P0": 0, "P1": 1, "P2": 2}

# kv key holding {alert_kind: last_sent_iso} for per-kind throttling.
ALERT_STATE_KEY = "alert_state"
# How many recent events one detect() pass scans (newest first).
EVENT_SCAN_LIMIT = 200
# Unattributed payments only count within the newest N events of that scan.
PAY_UNATTRIBUTED_WINDOW = 50
# This many simultaneously frozen agents is an incident on its own.
FROZEN_AGENTS_THRESHOLD = 2
# Freeze kinds that are alertable even for a single agent (never auto-thaw).
ESCALATED_FREEZE_KINDS = frozenset({"ethics", "circuit_breaker"})
WEBHOOK_TIMEOUT_S = 10.0


@dataclass(frozen=True)
class Alert:
    severity: str  # "P0" | "P1" | "P2"
    kind: str  # stable dedup/throttle key, e.g. "dead_letters"
    summary: str
    detail: dict

    def as_dict(self) -> dict[str, Any]:
        return {
            "severity": self.severity,
            "kind": self.kind,
            "summary": self.summary,
            "detail": dict(self.detail),
        }


def detect(world: World) -> list[Alert]:
    """Derive current incidents from world state. Pure read: no sends, no writes."""
    alerts: list[Alert] = []
    events = world.store.events(EVENT_SCAN_LIMIT)  # newest first

    breach = _detect_invariant_breach(world, events)
    if breach is not None:
        alerts.append(breach)

    comms = getattr(world, "comms", None)
    dead = int(comms.counts().get("dead", 0)) if comms is not None else 0
    if dead > 0:
        alerts.append(
            Alert(
                severity="P0",
                kind="dead_letters",
                summary=f"{dead} dead-lettered message(s) on the comms bus",
                detail={"dead": dead},
            )
        )

    if bool(world.broker.frozen):
        snap = world.broker.snapshot()
        reason = snap.get("halt_reason") or "unknown"
        alerts.append(
            Alert(
                severity="P1",
                kind="trading_halt",
                summary=f"trading halted: {reason}",
                detail={
                    "reason": reason,
                    "halted_at": snap.get("halted_at"),
                    "halt_tick": snap.get("halt_tick"),
                    "equity": snap.get("equity"),
                    "daily_pnl_pct": snap.get("daily_pnl_pct"),
                    "weekly_pnl_pct": snap.get("weekly_pnl_pct"),
                },
            )
        )

    frozen = _detect_frozen_agents(world)
    if frozen is not None:
        alerts.append(frozen)

    unattributed = sum(
        1
        for event in events[:PAY_UNATTRIBUTED_WINDOW]
        if event.get("kind") == "pay_unattributed"
    )
    if unattributed > 0:
        alerts.append(
            Alert(
                severity="P1",
                kind="payment_unattributed",
                summary=f"{unattributed} unattributed payment event(s) in recent history",
                detail={"count": unattributed, "window": PAY_UNATTRIBUTED_WINDOW},
            )
        )
    return alerts


def _detect_invariant_breach(
    world: World, events: list[dict[str, Any]]
) -> Alert | None:
    breach_events = sum(1 for event in events if event.get("kind") == "invariant_breach")
    from sovereign.capital.invariants import verify_invariants

    report = verify_invariants(world)
    failing = (
        [str(check.get("name")) for check in report.get("checks", []) if not check.get("ok")]
        if not report.get("ok", True)
        else []
    )
    if not breach_events and not failing:
        return None
    if failing:
        summary = f"ledger invariants failing: {', '.join(failing)}"
    else:
        summary = f"{breach_events} invariant_breach event(s) in recent history"
    return Alert(
        severity="P0",
        kind="invariant_breach",
        summary=summary,
        detail={"failing_checks": failing, "recent_breach_events": breach_events},
    )


def _detect_frozen_agents(world: World) -> Alert | None:
    kinds = {
        str(agent): str((world.freeze_info.get(agent) or {}).get("kind") or "unknown")
        for agent in sorted(world.frozen)
    }
    escalated = sorted(
        agent
        for agent, info in world.freeze_info.items()
        if isinstance(info, dict) and str(info.get("kind")) in ESCALATED_FREEZE_KINDS
    )
    if len(kinds) < FROZEN_AGENTS_THRESHOLD and not escalated:
        return None
    for agent in escalated:  # freeze_info should mirror frozen; never lose one
        kinds.setdefault(
            agent, str((world.freeze_info.get(agent) or {}).get("kind") or "unknown")
        )
    summary = f"{len(kinds)} agent(s) frozen"
    if escalated:
        summary += f"; escalated freezes: {', '.join(escalated)}"
    return Alert(
        severity="P1",
        kind="agents_frozen",
        summary=summary,
        detail={"agents": kinds, "escalated": escalated},
    )


class AlertManager:
    """Filter, throttle, and deliver alerts. ``dispatch`` never raises."""

    def __init__(self, config: AlertConfig) -> None:
        self.config = config

    def dispatch(self, world: World) -> list[Alert]:
        """Deliver every alert that passes the severity filter and throttle.

        Returns the alerts actually sent. Disabled config sends nothing.
        Any internal failure is swallowed: alerting must never crash a tick,
        the daemon loop, or a CLI caller.
        """
        if not self.config.enabled:
            return []
        sent: list[Alert] = []
        try:
            min_rank = SEVERITY_ORDER.get(self.config.min_severity, 0)
            candidates = [
                alert
                for alert in detect(world)
                if SEVERITY_ORDER.get(alert.severity, len(SEVERITY_ORDER)) <= min_rank
            ]
            if not candidates:
                return []
            raw_state = world.store.get_kv(ALERT_STATE_KEY)
            state: dict[str, Any] = dict(raw_state) if isinstance(raw_state, dict) else {}
            now = aware_utc(world.now)
            window = timedelta(minutes=max(0.0, float(self.config.throttle_minutes)))
            for alert in candidates:
                last_sent = parse_datetime(state.get(alert.kind))
                if last_sent is not None and now - last_sent < window:
                    continue
                if self._send(world, alert).get("outcome") == "sent":
                    state[alert.kind] = world.stamp()
                    world.store.set_kv(ALERT_STATE_KEY, state)
                    sent.append(alert)
        except Exception:  # noqa: BLE001 - alerting must never crash the caller
            return sent
        return sent

    def send_test(self, world: World) -> dict[str, Any]:
        """Push one synthetic P0 alert through the configured channel.

        Explicit operator action (`sovereign alerts --test`): it ignores
        ``enabled`` and the throttle so the channel itself can be verified,
        and never touches the throttle state a real alert would consult.
        """
        alert = Alert(
            severity="P0",
            kind="test_alert",
            summary="synthetic test alert from `sovereign alerts --test`",
            detail={"test": True, "firm": world.config.firm_name},
        )
        result = self._send(world, alert)
        return {
            "ok": result.get("outcome") == "sent",
            "alert": alert.as_dict(),
            **result,
        }

    def _send(self, world: World, alert: Alert) -> dict[str, Any]:
        """Deliver one alert; emit alert_sent/alert_error; never raise."""
        channel = self.config.channel
        try:
            if channel == "mail":
                if not self.config.to:
                    return {
                        "outcome": "skipped",
                        "channel": channel,
                        "reason": "no mail recipient configured (alerts.to)",
                    }
                body = (
                    f"{alert.summary}\n\n"
                    f"{json.dumps(alert.detail, indent=2, sort_keys=True, default=str)}\n\n"
                    f"firm: {world.config.firm_name}\nts: {world.stamp()}"
                )
                mailbox.send(
                    world,
                    to=self.config.to,
                    subject=f"[{alert.severity}] {alert.kind}",
                    body=body,
                    kind="alert",
                )
            elif channel == "webhook":
                if not self.config.webhook_url:
                    return {
                        "outcome": "skipped",
                        "channel": channel,
                        "reason": "no webhook url configured (alerts.webhook_url)",
                    }
                response = httpx.post(
                    self.config.webhook_url,
                    json={
                        "severity": alert.severity,
                        "kind": alert.kind,
                        "summary": alert.summary,
                        "detail": alert.detail,
                        "firm": world.config.firm_name,
                        "ts": world.stamp(),
                    },
                    timeout=WEBHOOK_TIMEOUT_S,
                )
                response.raise_for_status()
            else:  # config Literal forbids this; fail closed anyway
                return {
                    "outcome": "skipped",
                    "channel": channel,
                    "reason": f"unknown alert channel {channel!r}",
                }
        except Exception as exc:  # noqa: BLE001 - any delivery failure becomes an event
            error = self._redact(str(exc) or type(exc).__name__)[:200]
            self._emit(
                world,
                "alert_error",
                {
                    "severity": alert.severity,
                    "alert_kind": alert.kind,
                    "channel": channel,
                    "error": error,
                },
            )
            return {"outcome": "error", "channel": channel, "error": error}
        self._emit(
            world,
            "alert_sent",
            {
                "severity": alert.severity,
                "alert_kind": alert.kind,
                "channel": channel,
                "summary": alert.summary,
            },
        )
        return {"outcome": "sent", "channel": channel}

    def _redact(self, text: str) -> str:
        """Keep the configured endpoint (which may embed a token) out of errors."""
        for secret in (self.config.webhook_url or "", self.config.to or ""):
            if secret and secret in text:
                text = text.replace(secret, "[redacted]")
        return text

    @staticmethod
    def _emit(world: World, kind: str, payload: dict[str, Any]) -> None:
        try:
            world.store.emit(kind, payload, "operator")
        except Exception:  # noqa: BLE001, S110 - audit trail must never break delivery
            pass
