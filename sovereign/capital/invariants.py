"""Read-only financial invariants over the ledger, store, and paper broker.

Sign convention (from ``Ledger.balances`` / ``Store.ledger_balances``): every
posted row adds ``+amount`` to its debit account and ``-amount`` to its credit
account, so an account balance is (total debits - total credits).

- ``assets.*`` and ``expenses.*`` are debit-normal: value held / spent shows
  as a positive balance.
- ``liability.*``, ``equity.*``, and ``income.*`` are credit-normal: value
  owed / contributed / earned shows as a negative balance, so the held amount
  is the negated balance.

A purely in-ledger accounting identity (assets == liabilities + equity + net
income, all read from the same balances) is true for ANY subset of rows —
each row contributes ``+a`` and ``-a`` — so it cannot detect a deleted or
forged row. ``accounting_identity`` therefore anchors the unearned-revenue
liability to the store's open invoices, its independent source of truth
(``liability.unearned`` is the firm's only liability account today; any other
``liability.*`` accounts are still read from the ledger). When the books are
intact this is exactly the textbook identity; when a ledger row has been
tampered with, the asset side no longer balances against the anchored side.

Everything here is a pure read: no ledger posts, no kv writes, no events.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

from sovereign.memory.store import usd_amount

if TYPE_CHECKING:
    from sovereign.engine.world import World

__all__ = ["CHECK_NAMES", "TOLERANCE_USD", "TRADING_BOOK_TOLERANCE_USD", "verify_invariants"]

TOLERANCE_USD = 0.01
TRADING_BOOK_TOLERANCE_USD = 1.0

CHECK_NAMES = (
    "accounting_identity",
    "receivable_matches_open_invoices",
    "unearned_matches_open_invoices",
    "trading_book_matches_broker",
    "non_negative_cash",
)


def _check(name: str, ok: bool, detail: str) -> dict[str, Any]:
    return {"name": name, "ok": bool(ok), "detail": detail}


def verify_invariants(world: World) -> dict[str, Any]:
    """Cross-check ledger balances against store state and the paper broker.

    Returns ``{"ok": bool, "checks": [{"name", "ok", "detail"}, ...]}`` with
    one entry per check in :data:`CHECK_NAMES`, computed without writing
    anything anywhere.
    """
    balances = world.ledger.balances()

    def prefix_total(prefix: str) -> float:
        return float(sum(v for k, v in balances.items() if k.startswith(prefix)))

    assets = prefix_total("assets.")
    unearned_held = -float(balances.get("liability.unearned", 0.0))
    other_liabilities_held = -(prefix_total("liability.") - float(balances.get("liability.unearned", 0.0)))
    equity_held = -prefix_total("equity.")
    net_income = -prefix_total("income.") - prefix_total("expenses.")

    open_invoices = usd_amount(
        sum(float(inv.get("amount") or 0.0) for inv in world.store.invoices("open"))
    )

    checks: list[dict[str, Any]] = []

    identity_rhs = open_invoices + other_liabilities_held + equity_held + net_income
    identity_diff = assets - identity_rhs
    checks.append(
        _check(
            "accounting_identity",
            abs(identity_diff) <= TOLERANCE_USD,
            (
                f"assets={assets:.2f} vs liabilities(open invoices)={open_invoices:.2f}"
                f" + other_liabilities={other_liabilities_held:.2f}"
                f" + equity={equity_held:.2f} + net_income={net_income:.2f}"
                f" (ledger unearned={unearned_held:.2f}, diff={identity_diff:.4f})"
            ),
        )
    )

    receivable = float(balances.get("assets.receivable", 0.0))
    receivable_diff = receivable - open_invoices
    checks.append(
        _check(
            "receivable_matches_open_invoices",
            abs(receivable_diff) <= TOLERANCE_USD,
            (
                f"assets.receivable={receivable:.2f} vs open invoices={open_invoices:.2f}"
                f" (diff={receivable_diff:.4f})"
            ),
        )
    )

    unearned_diff = unearned_held - open_invoices
    checks.append(
        _check(
            "unearned_matches_open_invoices",
            abs(unearned_diff) <= TOLERANCE_USD,
            (
                f"liability.unearned={unearned_held:.2f} vs open invoices={open_invoices:.2f}"
                f" (diff={unearned_diff:.4f})"
            ),
        )
    )

    trading_book = float(balances.get("assets.trading_book", 0.0))
    broker_equity = float(world.broker.equity())
    if abs(trading_book) <= TOLERANCE_USD and abs(broker_equity) <= TOLERANCE_USD:
        book_ok = True
        book_detail = "trading book and broker equity are both zero"
    else:
        book_diff = trading_book - broker_equity
        book_ok = abs(book_diff) <= TRADING_BOOK_TOLERANCE_USD
        book_detail = (
            f"assets.trading_book={trading_book:.2f} vs broker equity={broker_equity:.2f}"
            f" (diff={book_diff:.4f}, tolerance={TRADING_BOOK_TOLERANCE_USD:.2f})"
        )
    checks.append(_check("trading_book_matches_broker", book_ok, book_detail))

    usdc = float(balances.get("assets.usdc", 0.0))
    checks.append(
        _check(
            "non_negative_cash",
            usdc >= -TOLERANCE_USD,
            f"assets.usdc={usdc:.2f}",
        )
    )

    return {"ok": all(c["ok"] for c in checks), "checks": checks}
