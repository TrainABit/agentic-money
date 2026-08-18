from types import SimpleNamespace

from sovereign.capital import onchain, payments
from sovereign.capital.invoice import issue
from sovereign.capital.onchain import IncomingTransfer
from sovereign.config import EngineConfig
from sovereign.engine.world import bootstrap
from sovereign.memory.store import Store


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
        "description": "onchain settlement test",
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


def _transfers(monkeypatch, eth=(), sol=()):
    """Route payments' log fetches to canned transfers; exceptions raise."""
    current = {"eth": list(eth), "sol": list(sol)}

    def fetch(chain):
        value = current[chain]
        if isinstance(value, BaseException):
            raise value
        return list(value)

    monkeypatch.setattr(onchain, "eth_incoming_usdc", lambda *a, **k: fetch("eth"))
    monkeypatch.setattr(onchain, "sol_incoming_usdc", lambda *a, **k: fetch("sol"))
    return current


def _duplicate_open_invoice(world, original, job_id, memo=None):
    """Bypass issue()'s open-amount uniqueness, like legacy duplicates did."""
    duplicate = dict(original)
    duplicate.update(
        {
            "id": f"inv_dup_{job_id}",
            "job_id": job_id,
            "status": "open",
            "path": "",
        }
    )
    if memo is not None:
        duplicate["memo"] = memo
    world.store.upsert_invoice(duplicate)
    return duplicate


def _settlement_rows(world, invoice_id):
    return [
        row
        for row in world.store.ledger_rows()
        if row["ref"] == invoice_id
        and (row["memo"].startswith("collect ") or row["memo"].startswith("recognize "))
    ]


def _events(world, kind, limit=200):
    return [event for event in world.store.events(limit) if event["kind"] == kind]


ETH_TXID = "0x" + "ab" * 32


def test_eth_transfer_settles_matching_invoice_and_dedupes(tmp_path, monkeypatch):
    world = bootstrap(_config(tmp_path))
    inv = issue(world, _job(world, "job_eth_logs", 100))
    _balances(monkeypatch)
    transfers = _transfers(monkeypatch)
    transfers["eth"] = [
        IncomingTransfer("eth", ETH_TXID, "0x" + "bb" * 20, 100_000_000, 900)
    ]

    got = payments.watch_and_collect(world)
    assert [item["id"] for item in got] == [inv["id"]]
    paid = world.store.get_invoice(inv["id"])
    assert paid["status"] == "paid"
    assert paid["paid_source"] == "chain:eth"
    assert len(_settlement_rows(world, inv["id"])) == 2
    assert world.store.get_kv("usdc_attributed") == 100
    assert world.store.chain_txid_seen("eth", ETH_TXID) is True
    recorded = world.store.chain_txids("eth")
    assert len(recorded) == 1
    assert recorded[0]["invoice_id"] == inv["id"]
    assert recorded[0]["amount_minor"] == 100_000_000
    assert recorded[0]["sender"] == "0x" + "bb" * 20

    # The same txid stays in the lookback window: nothing double-collects.
    assert payments.watch_and_collect(world) == []
    assert len(_settlement_rows(world, inv["id"])) == 2
    assert len(world.store.invoices("paid")) == 1
    assert (
        world.store.record_chain_txid("eth", ETH_TXID, 100_000_000, None, inv["id"])
        is False
    )
    assert len(world.store.chain_txids("eth")) == 1


def test_log_settlement_reserves_future_balance_delta(tmp_path, monkeypatch):
    """The balance fallback must not re-attribute funds already settled by txid."""
    world = bootstrap(_config(tmp_path))
    balances = _balances(monkeypatch)
    transfers = _transfers(monkeypatch)

    # Empty logs are not authoritative: the balance path establishes baselines.
    assert payments.watch_and_collect(world) == []
    assert _events(world, "pay_watch_baseline")

    inv_a = issue(world, _job(world, "job_reserve_a", 100))
    transfers["eth"] = [
        IncomingTransfer("eth", ETH_TXID, "0x" + "bb" * 20, 100_000_000, 901)
    ]
    got = payments.watch_and_collect(world)
    assert [item["id"] for item in got] == [inv_a["id"]]
    assert world.store.get_kv("usdc_manual_reserved") == 100

    # Same-amount invoice opens; logs go quiet; the settled funds now show up
    # as a balance delta. The reservation absorbs it instead of suspense.
    inv_b = issue(world, _job(world, "job_reserve_b", 100))
    transfers["eth"] = []
    balances["eth"] = 100
    assert payments.watch_and_collect(world) == []
    assert world.store.get_invoice(inv_b["id"])["status"] == "open"
    assert world.store.get_kv("usdc_suspense") == 0
    assert world.store.get_kv("usdc_manual_reserved") == 0
    assert _events(world, "pay_manual_reconciled")


def test_ambiguous_amount_leaves_every_invoice_open(tmp_path, monkeypatch):
    world = bootstrap(_config(tmp_path))
    first = issue(world, _job(world, "job_amb_1", 100))
    _job(world, "job_amb_2", 100)
    duplicate = _duplicate_open_invoice(world, first, "job_amb_2")
    _balances(monkeypatch)
    transfers = _transfers(monkeypatch)
    transfers["eth"] = [
        IncomingTransfer("eth", ETH_TXID, "0x" + "bb" * 20, 100_000_000, 902)
    ]

    assert payments.watch_and_collect(world) == []
    assert world.store.get_invoice(first["id"])["status"] == "open"
    assert world.store.get_invoice(duplicate["id"])["status"] == "open"
    ambiguous = _events(world, "pay_ambiguous")
    assert len(ambiguous) == 1
    assert sorted(ambiguous[0]["payload"]["invoice_ids"]) == sorted(
        [first["id"], duplicate["id"]]
    )
    assert ambiguous[0]["payload"]["txid"] == ETH_TXID
    recorded = world.store.chain_txids("eth")
    assert len(recorded) == 1
    assert recorded[0]["invoice_id"] is None

    # Recorded txid: the second sighting neither pays nor re-alerts.
    assert payments.watch_and_collect(world) == []
    assert world.store.get_invoice(first["id"])["status"] == "open"
    assert world.store.get_invoice(duplicate["id"])["status"] == "open"
    assert len(_events(world, "pay_ambiguous")) == 1


def test_unmatched_transfer_is_recorded_unattributed(tmp_path, monkeypatch):
    world = bootstrap(_config(tmp_path))
    issue(world, _job(world, "job_unmatched", 100))
    _balances(monkeypatch)
    transfers = _transfers(monkeypatch)
    transfers["eth"] = [
        IncomingTransfer("eth", ETH_TXID, "0x" + "bb" * 20, 55_000_000, 903)
    ]

    assert payments.watch_and_collect(world) == []
    unattributed = _events(world, "pay_unattributed")
    assert len(unattributed) == 1
    assert unattributed[0]["payload"]["reason"] == "no_exact_invoice"
    assert unattributed[0]["payload"]["txid"] == ETH_TXID
    recorded = world.store.chain_txids("eth")
    assert len(recorded) == 1
    assert recorded[0]["invoice_id"] is None
    assert payments.watch_and_collect(world) == []
    assert len(_events(world, "pay_unattributed")) == 1


def test_sol_transfer_settles_sol_invoice(tmp_path, monkeypatch):
    world = bootstrap(_config(tmp_path))
    inv = issue(world, _job(world, "job_sol_logs", 80))
    _balances(monkeypatch)
    transfers = _transfers(monkeypatch)
    transfers["sol"] = [
        IncomingTransfer("sol", "sig-sol-1", "PayerOwner1111", 80_000_000, 9000)
    ]

    got = payments.watch_and_collect(world)
    assert [item["id"] for item in got] == [inv["id"]]
    paid = world.store.get_invoice(inv["id"])
    assert paid["status"] == "paid"
    assert paid["paid_source"] == "chain:sol"
    recorded = world.store.chain_txids("sol")
    assert len(recorded) == 1
    assert recorded[0]["txid"] == "sig-sol-1"
    assert recorded[0]["invoice_id"] == inv["id"]
    assert payments.watch_and_collect(world) == []
    assert len(world.store.invoices("paid")) == 1


def test_memo_disambiguates_equal_amounts(tmp_path, monkeypatch):
    world = bootstrap(_config(tmp_path))
    first = issue(world, _job(world, "job_memo_1", 100))
    _job(world, "job_memo_2", 100)
    duplicate = _duplicate_open_invoice(world, first, "job_memo_2", memo="SOV-OTHERONE")
    _balances(monkeypatch)
    transfers = _transfers(monkeypatch)
    transfers["sol"] = [
        IncomingTransfer(
            "sol",
            "sig-memo-1",
            "PayerOwner1111",
            100_000_000,
            9001,
            memo=f"payment {first['memo']}",
        )
    ]

    got = payments.watch_and_collect(world)
    assert [item["id"] for item in got] == [first["id"]]
    assert world.store.get_invoice(first["id"])["status"] == "paid"
    assert world.store.get_invoice(duplicate["id"])["status"] == "open"
    assert _events(world, "pay_ambiguous") == []


def test_falls_back_to_balance_path_when_both_chains_error(tmp_path, monkeypatch):
    world = bootstrap(_config(tmp_path))
    assert world.config.chain.use_tx_logs is True
    inv = issue(world, _job(world, "job_fallback", 100))
    balances = _balances(monkeypatch)
    transfers = _transfers(monkeypatch)
    transfers["eth"] = RuntimeError("eth logs unavailable")
    transfers["sol"] = RuntimeError("sol logs unavailable")

    assert payments.watch_and_collect(world) == []  # baseline via balance path
    balances["eth"] = 100
    got = payments.watch_and_collect(world)
    assert [item["id"] for item in got] == [inv["id"]]
    assert world.store.get_invoice(inv["id"])["status"] == "paid"
    assert world.store.chain_txids() == []
    errors = _events(world, "pay_watch_error")
    assert errors and "logs unavailable" in errors[0]["payload"]["error"]


def test_use_tx_logs_false_never_fetches_logs(tmp_path, monkeypatch):
    config = _config(tmp_path)
    config.chain.use_tx_logs = False
    world = bootstrap(config)
    inv = issue(world, _job(world, "job_logs_off", 100))
    balances = _balances(monkeypatch)

    def forbidden(*_args, **_kwargs):
        raise AssertionError("log fetch must not run when use_tx_logs is False")

    monkeypatch.setattr(onchain, "eth_incoming_usdc", forbidden)
    monkeypatch.setattr(onchain, "sol_incoming_usdc", forbidden)

    assert payments.watch_and_collect(world) == []
    balances["eth"] = 100
    got = payments.watch_and_collect(world)
    assert [item["id"] for item in got] == [inv["id"]]


def test_store_chain_txid_dedup_primitives(tmp_path):
    store = Store(tmp_path / "chain.db")
    try:
        assert store.record_chain_txid("eth", "0x1", 5, "0xsender", "inv_a") is True
        # Same (chain, txid) dedups regardless of the other fields.
        assert store.record_chain_txid("eth", "0x1", 99, None, None) is False
        assert store.record_chain_txid("sol", "0x1", 5, None, None) is True
        assert store.chain_txid_seen("eth", "0x1") is True
        assert store.chain_txid_seen("eth", "0x2") is False
        rows = store.chain_txids()
        assert [(row["chain"], row["txid"]) for row in rows] == [
            ("sol", "0x1"),
            ("eth", "0x1"),
        ]
        assert rows[1]["amount_minor"] == 5
        assert rows[1]["sender"] == "0xsender"
        assert rows[1]["invoice_id"] == "inv_a"
        assert len(store.chain_txids("eth")) == 1
        assert len(store.chain_txids(limit=1)) == 1
    finally:
        store.close()


class _Response:
    def __init__(self, body):
        self._body = body

    def raise_for_status(self):
        return None

    def json(self):
        return self._body


def _fake_rpc(monkeypatch, handlers, calls):
    class Client:
        def __init__(self, **_kwargs):
            pass

        def __enter__(self):
            return self

        def __exit__(self, *_args):
            return None

        def post(self, _url, json=None):
            request = json or {}
            calls.append(request)
            result = handlers[request["method"]](request)
            return _Response(
                {"jsonrpc": "2.0", "id": request.get("id"), "result": result}
            )

    monkeypatch.setattr(onchain, "httpx", SimpleNamespace(Client=Client))


def test_malformed_eth_logs_are_skipped_without_raising(tmp_path, monkeypatch):
    token = "0xA0b86991c6218b36c1d19D4a2e9Eb0cE3606eB48"
    holder = "0x" + "aa" * 20
    holder_topic = "0x" + ("aa" * 20).rjust(64, "0")
    sender_topic = "0x" + ("bb" * 20).rjust(64, "0")
    topic = onchain.ERC20_TRANSFER_TOPIC
    valid = {
        "topics": [topic, sender_topic, holder_topic],
        "data": "0x" + format(100_000_000, "064x"),
        "blockNumber": "0x10",
        "transactionHash": "0x" + "AB" * 32,
    }
    malformed = [
        "not-a-dict",
        {  # missing recipient topic
            "topics": [topic, sender_topic],
            "data": "0x1",
            "blockNumber": "0x1",
            "transactionHash": "0x" + "c1" * 32,
        },
        {  # different event signature
            "topics": ["0x" + "11" * 32, sender_topic, holder_topic],
            "data": "0x1",
            "blockNumber": "0x1",
            "transactionHash": "0x" + "c2" * 32,
        },
        {  # sender topic is not a padded address
            "topics": [topic, "0xdeadbeef", holder_topic],
            "data": "0x1",
            "blockNumber": "0x1",
            "transactionHash": "0x" + "c3" * 32,
        },
        {  # non-hex amount
            "topics": [topic, sender_topic, holder_topic],
            "data": "0xzz",
            "blockNumber": "0x1",
            "transactionHash": "0x" + "c4" * 32,
        },
        {  # zero-amount spam transfer
            "topics": [topic, sender_topic, holder_topic],
            "data": "0x0",
            "blockNumber": "0x1",
            "transactionHash": "0x" + "c5" * 32,
        },
        {  # bad transaction hash
            "topics": [topic, sender_topic, holder_topic],
            "data": "0x1",
            "blockNumber": "0x1",
            "transactionHash": "0xshort",
        },
        dict(valid, transactionHash="0x" + "c6" * 32, removed=True),  # reorged
    ]
    calls: list = []
    _fake_rpc(
        monkeypatch,
        {
            "eth_blockNumber": lambda _req: hex(1000),
            "eth_getLogs": lambda _req: [valid, *malformed],
        },
        calls,
    )

    got = onchain.eth_incoming_usdc(
        "https://rpc.test", token, holder, lookback_blocks=500, confirmations=5
    )
    assert got == [
        IncomingTransfer("eth", "0x" + "ab" * 32, "0x" + "bb" * 20, 100_000_000, 16)
    ]
    logs_filter = calls[1]["params"][0]
    assert logs_filter["address"] == token
    assert logs_filter["topics"] == [topic, None, holder_topic]
    assert logs_filter["fromBlock"] == hex(1000 - 5 - 500)
    assert logs_filter["toBlock"] == hex(1000 - 5)


def test_sol_reader_diffs_token_balances_and_skips_unparseable(tmp_path, monkeypatch):
    owner = "OwnerPubkey111111111111111111111111111111"
    mint = "EPjFWdd5AufqSSqeM2qN1xzybapC8G4wEGGkZwyTDt1v"
    good_tx = {
        "slot": 1234,
        "meta": {
            "err": None,
            "preTokenBalances": [
                {
                    "mint": mint,
                    "owner": owner,
                    "uiTokenAmount": {"amount": "0", "decimals": 6},
                },
                {
                    "mint": mint,
                    "owner": "PayerOwner1111",
                    "uiTokenAmount": {"amount": "80000000", "decimals": 6},
                },
            ],
            "postTokenBalances": [
                {
                    "mint": mint,
                    "owner": owner,
                    "uiTokenAmount": {"amount": "80000000", "decimals": 6},
                },
                {
                    "mint": mint,
                    "owner": "PayerOwner1111",
                    "uiTokenAmount": {"amount": "0", "decimals": 6},
                },
            ],
        },
        "transaction": {
            "message": {
                "accountKeys": [{"pubkey": "FeePayer1111"}],
                "instructions": [{"program": "spl-memo", "parsed": "SOV-MEMO1234"}],
            }
        },
    }
    signatures = [
        {"signature": "sig-good", "err": None},
        {"signature": "sig-unfound", "err": None},
        {"signature": "sig-failed", "err": {"InstructionError": [0, "Custom"]}},
    ]
    transactions = {"sig-good": good_tx, "sig-unfound": None}
    calls: list = []
    _fake_rpc(
        monkeypatch,
        {
            "getSignaturesForAddress": lambda _req: signatures,
            "getTransaction": lambda req: transactions[req["params"][0]],
        },
        calls,
    )

    got = onchain.sol_incoming_usdc("https://sol.test", owner, mint, limit=10)
    assert got == [
        IncomingTransfer(
            "sol", "sig-good", "PayerOwner1111", 80_000_000, 1234, "SOV-MEMO1234"
        )
    ]
    fetched = [c["params"][0] for c in calls if c["method"] == "getTransaction"]
    assert fetched == ["sig-good", "sig-unfound"]  # failed signature never fetched
