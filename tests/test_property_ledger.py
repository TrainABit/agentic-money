"""Property-based tests for the double-entry ledger and the invoice lifecycle.

Every hypothesis example runs against a fresh Store (or a fresh sim world)
under its own unique directory, so examples stay independent and
deterministic. Monetary values are drawn as whole cents in $1.00..$10,000.00,
matching the store's usd_amount cent rounding, and asserted to the cent.

Sign convention (sovereign/capital/ledger.py, sovereign/capital/invariants.py):
a posted row adds +amount to its debit account and -amount to its credit
account, so assets/expenses are debit-normal (positive balances) and
liability/equity/income are credit-normal (negative balances); recognized
revenue is the negated income balance.
"""

from __future__ import annotations

import uuid
from typing import Any

from hypothesis import HealthCheck, given, settings
from hypothesis import strategies as st

from sovereign.capital.invariants import verify_invariants
from sovereign.capital.invoice import collect, issue, void
from sovereign.capital.ledger import ACCOUNTS, Ledger
from sovereign.config import EngineConfig
from sovereign.engine.world import World, bootstrap
from sovereign.memory.store import Store

CENT = 0.01

# Shared knobs: no per-example deadline (world bootstrap dominates), no local
# example database (keeps the worktree clean), and the function-scoped
# tmp_path fixture is safe because every example carves out a fresh uuid
# subdirectory under it.
RELAXED = {
    "deadline": None,
    "database": None,
    "suppress_health_check": [
        HealthCheck.function_scoped_fixture,
        HealthCheck.too_slow,
    ],
}

# Whole cents in $1.00 .. $10,000.00 so every drawn amount is exact.
amount_cents = st.integers(min_value=100, max_value=1_000_000)

balanced_postings = st.lists(
    st.tuples(
        st.sampled_from(ACCOUNTS), st.sampled_from(ACCOUNTS), amount_cents
    ).filter(lambda t: t[0] != t[1]),
    min_size=1,
    max_size=25,
)

# An op is ("issue", cents) or ("collect"|"void", index into issued invoices).
# collect on a paid/void invoice and void on a non-open invoice are documented
# no-ops, so every op over already-issued invoices is a valid sequence.
invoice_ops = st.lists(
    st.one_of(
        st.tuples(st.just("issue"), amount_cents),
        st.tuples(st.just("collect"), st.integers(min_value=0, max_value=15)),
        st.tuples(st.just("void"), st.integers(min_value=0, max_value=15)),
    ),
    min_size=1,
    max_size=10,
)


def _fresh_world(tmp_path) -> World:
    config = EngineConfig(mode="sim", data_dir=tmp_path / uuid.uuid4().hex)  # type: ignore[arg-type]
    return bootstrap(config)


def _delivered_job(world: World, job_id: str, cents: int) -> dict[str, Any]:
    job = {
        "id": job_id,
        "source": "manual",
        "title": f"Property work {job_id}",
        "status": "delivered",
        "price_usd": cents / 100,
        "fit": 0.9,
    }
    world.store.upsert_job(job)
    return world.store.get_job(job_id)


@settings(max_examples=50, **RELAXED)
@given(seq=balanced_postings)
def test_balanced_postings_keep_zero_sum(tmp_path, seq):
    """Any sequence of ledger.post() calls keeps sum(balances) at zero."""
    store = Store(tmp_path / uuid.uuid4().hex / "ledger.db")
    try:
        ledger = Ledger(store)
        for debit, credit, cents in seq:
            ledger.post(debit, credit, cents / 100, "property posting")
            assert abs(sum(ledger.balances().values())) < CENT
    finally:
        store.close()


@settings(max_examples=50, **RELAXED)
@given(cents=amount_cents, settle=st.booleans())
def test_issue_then_settle_recognizes_expected_revenue(tmp_path, cents, settle):
    """issue+collect recognizes exactly the invoice amount; issue+void
    recognizes nothing. Both clear the receivable back to its baseline."""
    world = _fresh_world(tmp_path)
    try:
        ledger = world.ledger
        revenue_before = ledger.revenue_by_prefix("income.labor")
        receivable_before = ledger.balance("assets.receivable")
        unearned_before = ledger.balance("liability.unearned")

        inv = issue(world, _delivered_job(world, "job_lifecycle", cents))
        amount = float(inv["amount"])
        # No other open invoices exist, so no uniqueness bump: billed == priced.
        assert abs(amount - cents / 100) < CENT
        # Billed, not earned: receivable up by the amount, revenue unchanged.
        assert abs(ledger.balance("assets.receivable") - receivable_before - amount) < CENT
        assert abs(ledger.revenue_by_prefix("income.labor") - revenue_before) < CENT
        assert verify_invariants(world)["ok"] is True

        if settle:
            assert collect(world, inv["id"], source="property")["status"] == "paid"
            expected_revenue = revenue_before + amount
        else:
            assert void(world, inv["id"], reason="property")["status"] == "void"
            expected_revenue = revenue_before

        assert abs(ledger.revenue_by_prefix("income.labor") - expected_revenue) < CENT
        assert abs(ledger.balance("assets.receivable") - receivable_before) < CENT
        assert abs(ledger.balance("liability.unearned") - unearned_before) < CENT
        assert verify_invariants(world)["ok"] is True
    finally:
        world.store.close()


@settings(max_examples=30, **RELAXED)
@given(ops=invoice_ops)
def test_random_invoice_sequences_preserve_invariants(tmp_path, ops):
    """verify_invariants stays ok after every step of any valid issue/collect/
    void sequence, and recognized labor revenue equals the paid invoices."""
    world = _fresh_world(tmp_path)
    try:
        issued: list[dict[str, Any]] = []
        for i, (op, value) in enumerate(ops):
            if op == "issue":
                issued.append(issue(world, _delivered_job(world, f"job_seq_{i}", value)))
            elif not issued:
                continue
            elif op == "collect":
                collect(world, issued[value % len(issued)]["id"], source="property")
            else:
                void(world, issued[value % len(issued)]["id"], reason="property")
            report = verify_invariants(world)
            assert report["ok"] is True, report["checks"]
            assert abs(sum(world.ledger.balances().values())) < CENT

        paid = sum(float(inv["amount"]) for inv in world.store.invoices("paid"))
        assert abs(world.ledger.revenue_by_prefix("income.labor") - paid) < CENT
    finally:
        world.store.close()
