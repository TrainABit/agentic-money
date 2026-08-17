from __future__ import annotations

from collections import defaultdict
from typing import Any

from sovereign.memory.store import Store

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
    ) -> None:
        if amount < 0:
            raise ValueError("amount must be >= 0")
        self.store.post_ledger(debit, credit, amount, memo, ref)

    def balances(self) -> dict[str, float]:
        bal: dict[str, float] = defaultdict(float)
        for row in self.store.ledger_rows():
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

    def revenue_by_prefix(self, prefix: str = "income.") -> float:
        """Income is credit-normal; signed balance is negative for net revenue."""
        b = self.balances()
        return round(sum(-v for k, v in b.items() if k.startswith(prefix)), 2)

    def expenses(self) -> float:
        b = self.balances()
        return round(sum(v for k, v in b.items() if k.startswith("expenses.")), 2)

    def snapshot(self) -> dict[str, Any]:
        b = self.balances()
        return {
            "balances": b,
            "equity_usd": self.equity_usd(),
            "revenue_usd": self.revenue_by_prefix(),
            "labor_usd": round(-b.get("income.labor", 0), 2),
            "trading_usd": round(-b.get("income.trading", 0), 2),
            "products_usd": round(-b.get("income.products", 0), 2),
            "retainers_usd": round(-b.get("income.retainers", 0), 2),
            "expenses_usd": self.expenses(),
        }
