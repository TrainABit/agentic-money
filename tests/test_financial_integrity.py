from concurrent.futures import ThreadPoolExecutor
import threading

import pytest

from sovereign.capital import payments
from sovereign.capital.invoice import collect, issue, void
from sovereign.config import EngineConfig
from sovereign.engine.world import bootstrap


def _config(tmp_path, mode="live"):
    return EngineConfig(
        mode=mode,
        data_dir=tmp_path,
        public_job_apis=False,
        fetch_market_data=False,
    )  # type: ignore[arg-type]


def _job(world, job_id, amount):
    job = {
        "id": job_id,
        "source": "manual",
        "title": f"Work for {job_id}",
        "description": "financial integrity test",
        "status": "delivered",
        "price_usd": amount,
        "fit": 0.9,
    }
    world.store.upsert_job(job)
    return world.store.get_job(job_id)


def _balances(monkeypatch, eth=0.0, sol=0.0):
    current = {"eth": eth, "sol": sol}

    def eth_balance(*_args, **_kwargs):
        value = current["eth"]
        if isinstance(value, BaseException):
            raise value
        return value

    def sol_balance(*_args, **_kwargs):
        value = current["sol"]
        if isinstance(value, BaseException):
            raise value
        return value

    monkeypatch.setattr(payments, "usdc_balance", eth_balance)
    monkeypatch.setattr(payments, "sol_usdc_balance", sol_balance)
    return current


def _settlement_rows(world, invoice_id):
    return [
        row
        for row in world.store.ledger_rows()
        if row["ref"] == invoice_id
        and (row["memo"].startswith("collect ") or row["memo"].startswith("recognize "))
    ]


def test_first_observation_is_baseline_not_revenue(tmp_path, monkeypatch):
    world = bootstrap(_config(tmp_path))
    inv = issue(world, _job(world, "job_baseline", 100))
    balances = _balances(monkeypatch, eth=100)

    assert payments.watch_and_collect(world) == []
    assert world.store.get_invoice(inv["id"])["status"] == "open"
    assert world.store.get_kv("usdc_suspense") == 0

    balances["eth"] = 200
    got = payments.watch_and_collect(world)
    assert [item["id"] for item in got] == [inv["id"]]
    assert world.store.get_invoice(inv["id"])["status"] == "paid"


def test_legacy_payment_state_migrates_once_without_reusing_balance(tmp_path, monkeypatch):
    world = bootstrap(_config(tmp_path))
    inv = issue(world, _job(world, "job_legacy_migration", 100))
    world.store.set_kv("usdc_onchain_eth", 100)
    world.store.set_kv("usdc_onchain_sol", 20)
    world.store.set_kv("usdc_onchain", 120)
    world.store.set_kv("usdc_attributed", 75)
    _balances(monkeypatch, eth=100, sol=20)

    assert payments.watch_and_collect(world) == []
    assert world.store.get_invoice(inv["id"])["status"] == "open"
    state = world.store.get_kv(payments.PAYMENT_STATE_KEY)
    assert state["chains"]["eth"]["initialized"] is True
    assert state["chains"]["eth"]["last_balance_minor"] == 100_000_000
    assert state["chains"]["sol"]["initialized"] is True
    assert state["chains"]["sol"]["last_balance_minor"] == 20_000_000
    assert state["legacy_aggregate_minor"] == 120_000_000
    assert state["historical_attributed_minor"] == 75_000_000
    assert world.store.get_kv("usdc_attributed") == 75

    assert payments.watch_and_collect(world) == []
    migrations = [
        event
        for event in world.store.events(100)
        if event["kind"] == "pay_watch_migrated"
    ]
    assert len(migrations) == 1


def test_legacy_aggregate_without_chain_values_still_baselines_first_poll(tmp_path, monkeypatch):
    world = bootstrap(_config(tmp_path))
    inv = issue(world, _job(world, "job_legacy_aggregate", 100))
    world.store.set_kv("usdc_onchain", 100)
    world.store.set_kv("usdc_attributed", 25)
    _balances(monkeypatch, eth=100, sol=0)

    assert payments.watch_and_collect(world) == []
    assert world.store.get_invoice(inv["id"])["status"] == "open"
    state = world.store.get_kv(payments.PAYMENT_STATE_KEY)
    assert state["legacy_aggregate_minor"] == 100_000_000
    assert state["historical_attributed_minor"] == 25_000_000
    migration = next(
        event
        for event in world.store.events(100)
        if event["kind"] == "pay_watch_migrated"
    )
    assert migration["payload"]["eth_baseline_usd"] is None
    assert migration["payload"]["sol_baseline_usd"] is None


def test_withdrawal_removes_suspense_and_redeposit_is_new_inflow(tmp_path, monkeypatch):
    world = bootstrap(_config(tmp_path))
    inv = issue(world, _job(world, "job_redeposit", 100))
    balances = _balances(monkeypatch)
    payments.watch_and_collect(world)

    balances["eth"] = 70
    assert payments.watch_and_collect(world) == []
    assert world.store.get_kv("usdc_suspense") == 70

    balances["eth"] = 0
    assert payments.watch_and_collect(world) == []
    assert world.store.get_kv("usdc_suspense") == 0

    balances["eth"] = 100
    assert [item["id"] for item in payments.watch_and_collect(world)] == [inv["id"]]
    assert len(_settlement_rows(world, inv["id"])) == 2


def test_manual_collection_consumes_suspense_and_reserves_future_delta(tmp_path, monkeypatch):
    world = bootstrap(_config(tmp_path))
    balances = _balances(monkeypatch)
    payments.watch_and_collect(world)
    manual = issue(world, _job(world, "job_manual", 100))

    balances["eth"] = 60
    assert payments.watch_and_collect(world) == []
    assert world.store.get_kv("usdc_suspense") == 60

    collect(world, manual["id"], source="cli")
    assert world.store.get_kv("usdc_suspense") == 0
    assert world.store.get_kv("usdc_manual_reserved") == 40

    other = issue(world, _job(world, "job_other", 40))
    balances["eth"] = 100
    assert payments.watch_and_collect(world) == []
    assert world.store.get_kv("usdc_manual_reserved") == 0
    assert world.store.get_invoice(other["id"])["status"] == "open"

    balances["eth"] = 140
    assert [item["id"] for item in payments.watch_and_collect(world)] == [other["id"]]


def test_ambiguous_exact_amount_stays_in_suspense(tmp_path, monkeypatch):
    world = bootstrap(_config(tmp_path))
    balances = _balances(monkeypatch)
    payments.watch_and_collect(world)
    first = issue(world, _job(world, "job_ambiguous_1", 100))
    second_job = _job(world, "job_ambiguous_2", 100)
    duplicate = dict(first)
    duplicate.update(
        {
            "id": "inv_legacy_duplicate",
            "job_id": second_job["id"],
            "status": "open",
            "path": "",
        }
    )
    world.store.upsert_invoice(duplicate)

    balances["eth"] = 100
    assert payments.watch_and_collect(world) == []
    assert world.store.get_invoice(first["id"])["status"] == "open"
    assert world.store.get_invoice(duplicate["id"])["status"] == "open"
    assert world.store.get_kv("usdc_suspense") == 100
    assert any(event["kind"] == "pay_ambiguous" for event in world.store.events(20))


def test_failed_chain_is_not_reused_and_other_chain_remains_live(tmp_path, monkeypatch):
    world = bootstrap(_config(tmp_path))
    inv = issue(world, _job(world, "job_stale_rpc", 100))
    balances = _balances(
        monkeypatch,
        eth=payments._ConfirmedBalance(100_000_000, 10),
        sol=0,
    )

    assert payments.watch_and_collect(world) == []
    balances["eth"] = payments._ConfirmedBalance(200_000_000, 9)
    assert payments.watch_and_collect(world) == []
    assert world.store.get_invoice(inv["id"])["status"] == "open"

    balances["eth"] = RuntimeError("stale upstream")
    assert payments.watch_and_collect(world) == []
    assert world.store.get_invoice(inv["id"])["status"] == "open"

    balances["sol"] = 100
    assert [item["id"] for item in payments.watch_and_collect(world)] == [inv["id"]]
    assert any(event["kind"] == "pay_watch_error" for event in world.store.events(20))


def test_json_rpc_error_payload_fails_closed(monkeypatch):
    class Response:
        def raise_for_status(self):
            return None

        def json(self):
            return {
                "jsonrpc": "2.0",
                "id": 1,
                "error": {"code": -32000, "message": "stale node"},
            }

    class Client:
        def __init__(self, **_kwargs):
            pass

        def __enter__(self):
            return self

        def __exit__(self, *_args):
            return None

        def post(self, *_args, **_kwargs):
            return Response()

    monkeypatch.setattr(payments.httpx, "Client", Client)
    with pytest.raises(RuntimeError, match="JSON-RPC error"):
        payments.usdc_balance("https://rpc.invalid", "0x1", "0x2")


def test_concurrent_collection_posts_one_settlement_pair(tmp_path):
    config = _config(tmp_path)
    first_world = bootstrap(config)
    inv = issue(first_world, _job(first_world, "job_concurrent", 123.45))
    second_world = bootstrap(config, heal=False)
    barrier = threading.Barrier(2)

    def settle(world):
        barrier.wait()
        return collect(world, inv["id"], source="operator")

    with ThreadPoolExecutor(max_workers=2) as pool:
        results = list(pool.map(settle, (first_world, second_world)))

    assert [item["status"] for item in results] == ["paid", "paid"]
    assert len(_settlement_rows(first_world, inv["id"])) == 2
    assert first_world.store.get_invoice(inv["id"])["status"] == "paid"
    assert second_world.ledger.balance("assets.usdc") == 123.45
    second_world.store.close()
    first_world.store.close()


def test_collection_rolls_back_every_write_after_injected_failure(tmp_path, monkeypatch):
    world = bootstrap(_config(tmp_path))
    balances = _balances(monkeypatch)
    payments.watch_and_collect(world)
    inv = issue(world, _job(world, "job_rollback", 88.88))
    balances["eth"] = 50
    assert payments.watch_and_collect(world) == []
    rows_before = len(world.store.ledger_rows())
    receivable_before = world.ledger.balance("assets.receivable")
    payment_state_before = world.store.get_kv(payments.PAYMENT_STATE_KEY)
    assert world.store.get_kv("usdc_suspense") == 50

    def fail_outcome(*_args, **_kwargs):
        raise RuntimeError("injected outcome failure")

    monkeypatch.setattr(world.store, "outcome", fail_outcome)
    with pytest.raises(RuntimeError, match="injected"):
        collect(world, inv["id"], source="test")

    assert world.store.get_invoice(inv["id"])["status"] == "open"
    assert world.store.get_job(inv["job_id"])["status"] == "invoiced"
    assert len(world.store.ledger_rows()) == rows_before
    assert world.ledger.balance("assets.receivable") == receivable_before
    assert _settlement_rows(world, inv["id"]) == []
    assert world.store.get_kv(payments.PAYMENT_STATE_KEY) == payment_state_before
    assert world.store.get_kv("usdc_suspense") == 50
    assert world.store.get_kv("usdc_manual_reserved") == 0
    assert not any(
        event["kind"] == "pay_manual_attribution"
        for event in world.store.events(100)
    )


def test_issue_and_void_roll_back_atomically(tmp_path, monkeypatch):
    world = bootstrap(_config(tmp_path, mode="sim"))
    job = _job(world, "job_issue_rollback", 75)
    original_upsert = world.store.upsert_invoice
    calls = 0

    def fail_final_invoice(record):
        nonlocal calls
        calls += 1
        if calls == 2:
            raise RuntimeError("injected invoice artifact failure")
        original_upsert(record)

    with monkeypatch.context() as patch:
        patch.setattr(world.store, "upsert_invoice", fail_final_invoice)
        with pytest.raises(RuntimeError, match="injected"):
            issue(world, job)

    assert world.store.invoice_for_job(job["id"]) is None
    assert world.store.get_job(job["id"])["status"] == "delivered"
    assert list(world.config.paths().invoices.iterdir()) == []

    inv = issue(world, world.store.get_job(job["id"]))
    receivable = world.ledger.balance("assets.receivable")

    def fail_void(record):
        if record.get("status") == "void":
            raise RuntimeError("injected void failure")
        original_upsert(record)

    monkeypatch.setattr(world.store, "upsert_invoice", fail_void)
    with pytest.raises(RuntimeError, match="injected"):
        void(world, inv["id"], reason="test")
    assert world.store.get_invoice(inv["id"])["status"] == "open"
    assert world.ledger.balance("assets.receivable") == receivable


def test_open_invoice_amounts_are_unique_by_cent(tmp_path):
    world = bootstrap(_config(tmp_path, mode="sim"))
    first = issue(world, _job(world, "job_unique_1", 100))
    second = issue(world, _job(world, "job_unique_2", 100))

    assert first["amount"] == 100
    assert second["amount"] == 100.01
    assert first["metadata"]["quoted_amount_usd"] == 100
    assert second["metadata"]["quoted_amount_usd"] == 100
    assert second["metadata"]["amount_adjustment_usd"] == 0.01


def test_hot_wallet_cap_breach_is_unhealthy(tmp_path):
    config = _config(tmp_path)
    config.risk.hot_wallet_cap_usd = 150
    world = bootstrap(config)
    world.ledger.post("assets.usdc", "equity.treasury", 150.01, "fund hot wallet")

    status = world.treasury.policy_status()
    assert status["hot_wallet_usd"] == 150.01
    assert status["hot_wallet_breach"] is True
    assert status["within_hot_wallet_cap"] is False
    assert status["healthy"] is False
    assert status["health"] == "breach"
