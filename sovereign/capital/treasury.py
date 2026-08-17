from __future__ import annotations

from typing import Any

from sovereign.capital.ledger import Ledger
from sovereign.config import EngineConfig


class Treasury:
    def __init__(self, ledger: Ledger, config: EngineConfig) -> None:
        self.ledger = ledger
        self.config = config

    def operating_cash(self) -> float:
        b = self.ledger.balances()
        return round(b.get("assets.cash_usd", 0) + b.get("assets.usdc", 0), 2)

    def trading_book(self) -> float:
        return round(self.ledger.balance("assets.trading_book"), 2)

    def can_spend(self, usd: float, from_trading: bool = False) -> tuple[bool, str]:
        if usd <= 0:
            return False, "non-positive spend"
        if from_trading:
            if usd > self.trading_book():
                return False, "trading book too small"
            return True, "ok"
        if usd > self.operating_cash():
            return False, "insufficient operating cash"
        return True, "ok"

    def allocate_trading(self, usd: float, reason: str) -> bool:
        """Move USDC into the walled trading book. Never the other way without Treasurer memo."""
        if usd <= 0:
            return False
        if self.operating_cash() - usd < 0:
            return False
        # Keep a labor buffer: do not allocate more than 20% of cash until minimum target is hit
        snap = self.ledger.snapshot()
        if snap["revenue_usd"] < self.config.goals.minimum_usd:
            cap = self.operating_cash() * 0.10
            if usd > cap:
                return False
        self.ledger.post(
            "assets.trading_book",
            "assets.usdc",
            usd,
            reason,
            ref="alloc_trading",
        )
        return True

    def receive(self, usd: float, source: str, income_account: str, ref: str | None = None, ts: str | None = None) -> None:
        self.ledger.post("assets.usdc", income_account, usd, f"receive {source}", ref, ts=ts)

    def pay(self, usd: float, expense_account: str, memo: str, ref: str | None = None, ts: str | None = None) -> bool:
        ok, _ = self.can_spend(usd)
        if not ok:
            return False
        self.ledger.post(expense_account, "assets.usdc", usd, memo, ref, ts=ts)
        return True

    def policy_status(self) -> dict[str, Any]:
        return {
            "operating_cash": self.operating_cash(),
            "trading_book": self.trading_book(),
            "hot_wallet_cap": self.config.risk.hot_wallet_cap_usd,
            "equity": self.ledger.equity_usd(),
        }
