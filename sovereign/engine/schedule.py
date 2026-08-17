from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Any, Protocol


class Clock(Protocol):
    """Source of timezone-aware wall time."""

    def now(self) -> datetime: ...


class SystemClock:
    def now(self) -> datetime:
        return datetime.now(timezone.utc)


def aware_utc(value: datetime) -> datetime:
    if value.tzinfo is None:
        return value.replace(tzinfo=timezone.utc)
    return value.astimezone(timezone.utc)


def parse_datetime(value: Any) -> datetime | None:
    if isinstance(value, datetime):
        return aware_utc(value)
    if not isinstance(value, str) or not value:
        return None
    try:
        return aware_utc(datetime.fromisoformat(value))
    except ValueError:
        return None


def elapsed(now: datetime, since: Any) -> timedelta | None:
    started = parse_datetime(since)
    if started is None:
        return None
    return max(timedelta(0), aware_utc(now) - started)


def elapsed_days(now: datetime, since: Any) -> float | None:
    age = elapsed(now, since)
    return None if age is None else age.total_seconds() / 86_400.0


@dataclass
class Scheduler:
    """Persistent live cadences with unchanged tick modulo semantics in simulation."""

    store: Any
    mode: str
    state_key: str = "schedule"

    def due(
        self,
        key: str,
        *,
        now: datetime,
        tick: int,
        sim_every_ticks: int,
        live_every: timedelta,
    ) -> bool:
        if self.mode == "sim":
            return tick % max(1, sim_every_ticks) == 0
        state = dict(self.store.get_kv(self.state_key) or {})
        entry = state.get(key)
        if isinstance(entry, dict):
            retry_at = parse_datetime(entry.get("next"))
            if retry_at is not None:
                return aware_utc(now) >= retry_at
            entry = entry.get("last")
        since = parse_datetime(entry)
        return since is None or aware_utc(now) - since >= live_every

    def mark(self, key: str, *, now: datetime) -> None:
        if self.mode == "sim":
            return
        state = dict(self.store.get_kv(self.state_key) or {})
        state[key] = aware_utc(now).isoformat()
        self.store.set_kv(self.state_key, state)

    def retry_after(self, key: str, *, now: datetime, delay: timedelta) -> None:
        """Replace a normal cadence with a bounded retry deadline after failure."""
        if self.mode == "sim":
            return
        state = dict(self.store.get_kv(self.state_key) or {})
        current = state.get(key)
        last = current.get("last") if isinstance(current, dict) else current
        state[key] = {
            "last": last or aware_utc(now).isoformat(),
            "next": (aware_utc(now) + max(timedelta(0), delay)).isoformat(),
        }
        self.store.set_kv(self.state_key, state)

    def claim(
        self,
        key: str,
        *,
        now: datetime,
        tick: int,
        sim_every_ticks: int,
        live_every: timedelta,
    ) -> bool:
        if not self.due(
            key,
            now=now,
            tick=tick,
            sim_every_ticks=sim_every_ticks,
            live_every=live_every,
        ):
            return False
        self.mark(key, now=now)
        return True
