from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timedelta
from typing import Any

from sovereign.config import RiskLimits
from sovereign.engine.schedule import aware_utc


@dataclass
class PaperBroker:
    cash: float
    position: float = 0.0
    last_price: float = 0.0
    equity_high: float = 0.0
    day_start_equity: float = 0.0
    week_start_equity: float = 0.0
    frozen: bool = False
    halt_tick: int | None = None
    halted_at: datetime | None = None
    halt_reason: str | None = None
    day_key: str = ""
    week_key: str = ""
    fills: list[dict[str, Any]] = field(default_factory=list)

    def mark(self, price: float) -> float:
        self.last_price = float(price)
        eq = self.equity()
        if self.equity_high <= 0:
            self.equity_high = eq
        if self.day_start_equity <= 0:
            self.day_start_equity = eq
        if self.week_start_equity <= 0:
            self.week_start_equity = eq
        self.equity_high = max(self.equity_high, eq)
        return eq

    def roll_windows(self, now: datetime) -> None:
        now = aware_utc(now)
        day = now.date().isoformat()
        week = f"{now.isocalendar().year}-W{now.isocalendar().week:02d}"
        eq = self.equity() if self.last_price or self.cash else self.day_start_equity
        if self.day_key != day:
            self.day_key = day
            self.day_start_equity = eq if eq else self.day_start_equity
        if self.week_key != week:
            self.week_key = week
            self.week_start_equity = eq if eq else self.week_start_equity

    def equity(self) -> float:
        return self.cash + self.position * self.last_price

    def daily_pnl_pct(self) -> float:
        if self.day_start_equity <= 0:
            return 0.0
        return (self.equity() - self.day_start_equity) / self.day_start_equity

    def weekly_pnl_pct(self) -> float:
        if self.week_start_equity <= 0:
            return 0.0
        return (self.equity() - self.week_start_equity) / self.week_start_equity

    def maybe_halt(
        self,
        limits: RiskLimits,
        tick: int = 0,
        now: datetime | None = None,
    ) -> str | None:
        if self.frozen:
            return None
        if self.daily_pnl_pct() <= -limits.daily_halt_pct:
            self.frozen = True
            self.halt_tick = tick
            self.halted_at = aware_utc(now) if now is not None else None
            self.halt_reason = "daily_halt"
            return self.halt_reason
        elif self.weekly_pnl_pct() <= -limits.weekly_halt_pct:
            self.frozen = True
            self.halt_tick = tick
            self.halted_at = aware_utc(now) if now is not None else None
            self.halt_reason = "weekly_halt"
            return self.halt_reason
        return None

    def maybe_unfreeze(
        self,
        limits: RiskLimits,
        tick: int = 0,
        cooldown: int | timedelta = 5,
        now: datetime | None = None,
    ) -> bool:
        if not self.frozen:
            return False
        if now is not None:
            if self.halted_at is None or not isinstance(cooldown, timedelta):
                return False
            if aware_utc(now) - aware_utc(self.halted_at) < cooldown:
                return False
        else:
            tick_cooldown = int(cooldown.total_seconds()) if isinstance(cooldown, timedelta) else cooldown
            if self.halt_tick is None or tick - int(self.halt_tick) < tick_cooldown:
                return False
        if self.daily_pnl_pct() > -limits.daily_halt_pct and self.weekly_pnl_pct() > -limits.weekly_halt_pct:
            self.frozen = False
            self.halt_tick = None
            self.halted_at = None
            self.halt_reason = None
            return True
        return False

    def target_position(self, desired_notional: float, price: float, cost: float) -> dict[str, Any]:
        if self.frozen or price <= 0:
            return {"ok": False, "reason": "frozen_or_bad_price"}
        desired_qty = desired_notional / price
        delta = desired_qty - self.position
        notional = abs(delta) * price
        fee = notional * cost
        self.cash -= delta * price
        self.cash -= fee
        self.position += delta
        fill = {
            "price": price,
            "delta": delta,
            "fee": fee,
            "position": self.position,
            "cash": self.cash,
            "equity": self.equity(),
        }
        self.fills.append(fill)
        return {"ok": True, **fill}

    def snapshot(self) -> dict[str, Any]:
        return {
            "cash": round(self.cash, 4),
            "position": round(self.position, 8),
            "last_price": self.last_price,
            "equity": round(self.equity(), 4),
            "frozen": self.frozen,
            "daily_pnl_pct": round(self.daily_pnl_pct(), 4),
            "weekly_pnl_pct": round(self.weekly_pnl_pct(), 4),
            "day_start_equity": self.day_start_equity,
            "week_start_equity": self.week_start_equity,
            "day_key": self.day_key,
            "week_key": self.week_key,
            "halt_tick": self.halt_tick,
            "halted_at": self.halted_at.isoformat() if self.halted_at is not None else None,
            "halt_reason": self.halt_reason,
            "fills": len(self.fills),
        }
