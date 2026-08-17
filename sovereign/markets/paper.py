from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

import numpy as np

from sovereign.config import RiskLimits


@dataclass
class PaperBroker:
    cash: float
    position: float = 0.0
    last_price: float = 0.0
    equity_high: float = 0.0
    day_start_equity: float = 0.0
    week_start_equity: float = 0.0
    frozen: bool = False
    fills: list[dict[str, Any]] = field(default_factory=list)

    def mark(self, price: float) -> float:
        self.last_price = float(price)
        eq = self.equity()
        if self.equity_high <= 0:
            self.equity_high = eq
            self.day_start_equity = eq
            self.week_start_equity = eq
        self.equity_high = max(self.equity_high, eq)
        return eq

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

    def maybe_halt(self, limits: RiskLimits) -> str | None:
        if self.daily_pnl_pct() <= -limits.daily_halt_pct:
            self.frozen = True
            return "daily_halt"
        if self.weekly_pnl_pct() <= -limits.weekly_halt_pct:
            self.frozen = True
            return "weekly_halt"
        return None

    def target_position(self, desired_notional: float, price: float, cost: float) -> dict[str, Any]:
        if self.frozen or price <= 0:
            return {"ok": False, "reason": "frozen_or_bad_price"}
        desired_qty = desired_notional / price
        delta = desired_qty - self.position
        notional = abs(delta) * price
        fee = notional * cost
        # Pay fee from cash; if going long, spend cash for coins
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
            "fills": len(self.fills),
        }
