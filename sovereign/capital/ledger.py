from __future__ import annotations

from collections import defaultdict
from datetime import datetime, timedelta, timezone
from typing import Any

from sovereign.memory.store import Store, iso

ACCOUNTS = (
    "assets.cash_usd",
    "assets.usdc",
    "assets.btc",
    "assets.eth",
    "assets.trading_book",
    "assets.receivable",
    "income.labor",
    "income.trading",
    "income.products",
    "income.retainers",
    "expenses.infra",
    "expenses.fees",
    "expenses.tools",
    "equity.treasury",
)


class Ledger:
    """Double-entry ledger. Debit asset to increase it; credit income to recognize revenue."""

    def __init__(self, store: Store) -> None:
        self.store = store

    def post(
        self,
        debit: str,
        credit: str,
        amount: float,
        memo: str,
        ref: str | None = None,
        ts: str | None = None,
    ) -> None:
        if amount < 0:
            raise ValueError("amount must be >= 0")
        self.store.post_ledger(debit, credit, amount, memo, ref, ts=ts)

    def balances(self, since: str | None = None) -> dict[str, float]:
        bal: dict[str, float] = defaultdict(float)
        for row in self.store.ledger_rows():
            if since and str(row["ts"]) < since:
                continue
            bal[row["debit"]] += float(row["amount"])
            bal[row["credit"]] -= float(row["amount"])
        return dict(bal)

    def balance(self, account: str) -> float:
        return float(self.balances().get(account, 0.0))

    def equity_usd(self) -> float:
        b = self.balances()
        assets = (
            b.get("assets.cash_usd", 0)
            + b.get("assets.usdc", 0)
            + b.get("assets.btc", 0)
            + b.get("assets.eth", 0)
            + b.get("assets.trading_book", 0)
            + b.get("assets.receivable", 0)
        )
        return round(assets, 2)

    def revenue_by_prefix(self, prefix: str = "income.", since: str | None = None) -> float:
        b = self.balances(since=since)
        return round(sum(-v for k, v in b.items() if k.startswith(prefix)), 2)

    def trailing_revenue(self, days: int = 30, now: datetime | None = None) -> float:
        now = now or datetime.now(timezone.utc)
        since = iso(now - timedelta(days=days))
        return self.revenue_by_prefix(since=since)

    def expenses(self) -> float:
        b = self.balances()
        return round(sum(v for k, v in b.items() if k.startswith("expenses.")), 2)

    def snapshot(self, now: datetime | None = None) -> dict[str, Any]:
        b = self.balances()
        trailing = self.trailing_revenue(30, now=now)
        return {
            "balances": b,
            "equity_usd": self.equity_usd(),
            "revenue_usd": self.revenue_by_prefix(),
            "trailing_30d_usd": trailing,
            "labor_usd": round(-b.get("income.labor", 0), 2),
            "trading_usd": round(-b.get("income.trading", 0), 2),
            "products_usd": round(-b.get("income.products", 0), 2),
            "retainers_usd": round(-b.get("income.retainers", 0), 2),
            "expenses_usd": self.expenses(),
        }
