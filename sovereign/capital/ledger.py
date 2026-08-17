from __future__ import annotations

from datetime import datetime, timedelta, timezone
from typing import Any

from sovereign.memory.store import Store, iso, usd_amount

ACCOUNTS = (
    "assets.cash_usd",
    "assets.usdc",
    "assets.btc",
    "assets.eth",
    "assets.trading_book",
    "assets.receivable",
    "liability.unearned",
    "income.labor",
    "income.trading",
    "income.trading_paper",
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

        self._bal_cache: dict[str, float] | None = None
        self._bal_cache_version: tuple[int, int] | None = None

    def post(
        self,
        debit: str,
        credit: str,
        amount: float,
        memo: str,
        ref: str | None = None,
        ts: str | None = None,
    ) -> None:
        if float(amount) < 0:
            raise ValueError("amount must be >= 0")
        amount = usd_amount(amount)
        self._bal_cache = None
        self._bal_cache_version = None
        self.store.post_ledger(debit, credit, amount, memo, ref, ts=ts)

    def balances(self, since: str | None = None) -> dict[str, float]:
        if since is not None:
            return self.store.ledger_balances(since=since)
        version = self.store.ledger_version()
        if self._bal_cache is not None and self._bal_cache_version == version:
            return dict(self._bal_cache)

        # Retry if another Store connection commits between the version check
        # and SQL aggregation; never stamp old balances with a newer version.
        out: dict[str, float] = {}
        for _ in range(3):
            before = self.store.ledger_version()
            out = self.store.ledger_balances()
            after = self.store.ledger_version()
            if before == after:
                self._bal_cache = dict(out)
                self._bal_cache_version = after
                break
        else:
            self._bal_cache = None
            self._bal_cache_version = None
        return out

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
        liabilities = sum(-v for k, v in b.items() if k.startswith("liability."))
        return usd_amount(assets - liabilities)

    def revenue_by_prefix(self, prefix: str = "income.", since: str | None = None) -> float:
        b = self.balances(since=since)
        return usd_amount(
            sum(
                -v
                for k, v in b.items()
                if k.startswith(prefix) and not k.endswith("_paper") and "unearned" not in k
            )
        )

    def trailing_revenue(self, days: int = 30, now: datetime | None = None) -> float:
        now = now or datetime.now(timezone.utc)
        since = iso(now - timedelta(days=days))
        return self.revenue_by_prefix(since=since)

    def expenses(self) -> float:
        b = self.balances()
        return usd_amount(sum(v for k, v in b.items() if k.startswith("expenses.")))

    def snapshot(self, now: datetime | None = None) -> dict[str, Any]:
        b = self.balances()
        trailing = self.trailing_revenue(30, now=now)
        return {
            "balances": b,
            "equity_usd": self.equity_usd(),
            "revenue_usd": self.revenue_by_prefix(),
            "trailing_30d_usd": trailing,
            "labor_usd": usd_amount(-b.get("income.labor", 0)),
            "trading_usd": usd_amount(-b.get("income.trading", 0)),
            "products_usd": usd_amount(-b.get("income.products", 0)),
            "retainers_usd": usd_amount(-b.get("income.retainers", 0)),
            "expenses_usd": self.expenses(),
        }
