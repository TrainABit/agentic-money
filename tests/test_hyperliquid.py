"""Hyperliquid venue: fake client, fail-closed live gate, live ledger account."""

from __future__ import annotations

import json

import numpy as np
import pytest

from sovereign.agents.roles import trader
from sovereign.cli import main
from sovereign.config import EngineConfig
from sovereign.engine.world import bootstrap, load_prices
from sovereign.markets.hyperliquid import (
    FakeHyperliquid,
    HyperliquidBroker,
    HyperliquidClient,
    HyperliquidError,
    build_broker,
    fetch_hyperliquid_closes,
)
from sovereign.markets.paper import PaperBroker
from sovereign.ops import readiness


def test_fake_mids_candles_and_orders():
    fake = FakeHyperliquid.with_synthetic_candles(n=500, coin="BTC")
    client = HyperliquidClient(testnet=True, fake=fake)
    mids = client.all_mids()
    assert mids["BTC"] > 0
    closes = fetch_hyperliquid_closes("BTC", client=client)
    assert len(closes) >= 480
    assert np.all(closes > 0)
    fill = client.market_order("BTC", True, 0.01)
    assert fill["ok"] is True
    assert fake.orders[-1]["sz"] == 0.01
    with pytest.raises(HyperliquidError, match="withdrawals"):
        client.withdraw(1.0)
    with pytest.raises(HyperliquidError, match="transfers"):
        client.usd_transfer(1.0)
    with pytest.raises(HyperliquidError, match="vault"):
        client.vault_transfer(1.0)
    assert "eth_key" not in repr(client)
    assert "_eth_key" not in json.dumps(fill)


def test_build_broker_fail_closed(tmp_path):
    sim = EngineConfig(mode="sim", data_dir=tmp_path / "sim")
    sim.trading.venue = "hyperliquid"
    assert isinstance(build_broker(sim), PaperBroker)
    assert build_broker(sim).venue == "paper"

    live = EngineConfig(mode="live", data_dir=tmp_path / "live", fetch_market_data=False)
    live.trading.venue = "hyperliquid"
    paperish = build_broker(live)
    assert paperish.venue == "hyperliquid"
    assert not isinstance(paperish, HyperliquidBroker)

    live.trading.hyperliquid_enabled = True
    live.trading.hyperliquid_fake = True
    live_broker = build_broker(live)
    assert isinstance(live_broker, HyperliquidBroker)
    assert live_broker.client is not None

    live.trading.hyperliquid_testnet = False
    live.trading.hyperliquid_allow_mainnet = False
    with pytest.raises(HyperliquidError, match="hyperliquid_allow_mainnet"):
        build_broker(live)


def test_hyperliquid_broker_targets_from_mid():
    fake = FakeHyperliquid(mids={"BTC": 50_000.0})
    client = HyperliquidClient(testnet=True, fake=fake)
    broker = HyperliquidBroker(cash=1_000.0, client=client, coin="BTC", min_order_usd=10.0)
    fill = broker.target_position(500.0, 1.0, 0.0)
    assert fill["ok"] is True
    assert fill["venue"] == "hyperliquid"
    assert abs(broker.position * 50_000.0 - 500.0) < 1e-6
    tiny = broker.target_position(broker.position * 50_000.0 + 1.0, 50_000.0, 0.0)
    assert tiny["ok"] is False
    assert tiny["reason"] == "below_min_order"


def test_trader_skips_when_hyperliquid_disabled(tmp_path):
    cfg = EngineConfig(mode="live", data_dir=tmp_path, fetch_market_data=False)
    cfg.trading.venue = "hyperliquid"
    cfg.trading.hyperliquid_enabled = False
    world = bootstrap(cfg, heal=False)
    world.broker.cash = 1_000
    world.certified = [
        {"strategy_id": "tsmom_vol", "certified": True, "oos": {"sharpe": 2.0}}
    ]
    world.market_close = np.linspace(100.0, 200.0, 300).tolist()
    result = trader(world)[0]
    assert result["skipped"] == "hyperliquid_disabled"


def test_trader_live_hyperliquid_posts_income_trading(tmp_path):
    cfg = EngineConfig(mode="live", data_dir=tmp_path, fetch_market_data=False)
    cfg.trading.venue = "hyperliquid"
    cfg.trading.hyperliquid_enabled = True
    cfg.trading.hyperliquid_fake = True
    cfg.risk.hot_wallet_cap_usd = 200
    cfg.risk.trading_risk_per_signal = 1.0
    world = bootstrap(cfg, heal=False)
    assert isinstance(world.broker, HyperliquidBroker)
    world.broker.cash = 10_000
    world.market_close = np.linspace(100.0, 500.0, 300).tolist()
    if world.broker.client and world.broker.client.fake:
        world.broker.client.fake.mids["BTC"] = float(world.market_close[-1])
    world.certified = [
        {"strategy_id": "tsmom_vol", "certified": True, "oos": {"sharpe": 1.8}},
    ]
    world.store.set_kv("trader_last_eq", 0)
    result = trader(world)[0]
    assert result.get("skipped") is None
    assert result["strategy"] == "tsmom_vol"
    assert result["fill"]["ok"] is True
    assert abs(world.ledger.balance("income.trading")) > 0
    assert abs(world.ledger.balance("income.trading_paper")) == 0


def test_load_prices_uses_hyperliquid_fake(tmp_path):
    cfg = EngineConfig(mode="live", data_dir=tmp_path, fetch_market_data=True)
    cfg.trading.venue = "hyperliquid"
    cfg.trading.hyperliquid_fake = True
    world = bootstrap(cfg, heal=False)
    load_prices(world, force=True)
    assert world.last_prices["source"] == "hyperliquid"
    assert len(world.market_close) >= 480


def test_cli_trading_and_readiness_never_leak_keys(tmp_path, capsys):
    code = main(["init", "--data-dir", str(tmp_path), "--mode", "sim"])
    assert code == 0
    capsys.readouterr()
    code = main(["trading", "--data-dir", str(tmp_path), "--mode", "sim"])
    assert code == 0
    payload = json.loads(capsys.readouterr().out)
    blob = json.dumps(payload)
    assert payload["venue"] == "paper"
    assert payload["coin"] == "BTC"
    assert "eth_key" not in blob
    assert "mnemonic" not in blob
    assert "private_key" not in blob

    world = bootstrap(EngineConfig(mode="sim", data_dir=tmp_path), heal=False)
    report = readiness(world)
    by_name = {check["name"]: check for check in report["checks"]}
    assert by_name["trading"]["required"] is False
    assert by_name["trading"]["ok"] is True
    assert by_name["trading"]["detail"]["venue"] == "paper"
