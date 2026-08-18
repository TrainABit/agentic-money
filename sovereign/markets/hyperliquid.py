"""Hyperliquid live execution.

Sim always stays on :class:`PaperBroker`. Live trading is fail-closed:
``trading.hyperliquid_enabled`` defaults false, testnet defaults true, and
mainnet additionally requires ``hyperliquid_allow_mainnet``.

This module never withdraws, never transfers, and never talks to a vault.
Tests use :class:`FakeHyperliquid`; the official SDK is imported only when
a live (non-fake) order is placed.
"""

from __future__ import annotations

import os
import time
from dataclasses import dataclass, field
from typing import Any

import httpx
import numpy as np

from sovereign.config import EngineConfig
from sovereign.markets.data import synthetic_ohlc, validate_closes
from sovereign.markets.paper import PaperBroker


INFO_TESTNET = "https://api.hyperliquid-testnet.xyz/info"
INFO_MAINNET = "https://api.hyperliquid.xyz/info"
EXCHANGE_TESTNET = "https://api.hyperliquid-testnet.xyz"
EXCHANGE_MAINNET = "https://api.hyperliquid.xyz"

class HyperliquidError(RuntimeError):
    """Venue configuration or protocol error (never a fill)."""


def _use_fake(trading: Any) -> bool:
    return bool(getattr(trading, "hyperliquid_fake", False)) or os.environ.get(
        "SOVEREIGN_HL_FAKE"
    ) == "1"


def default_info_url(*, testnet: bool) -> str:
    return INFO_TESTNET if testnet else INFO_MAINNET


def default_exchange_url(*, testnet: bool) -> str:
    return EXCHANGE_TESTNET if testnet else EXCHANGE_MAINNET


@dataclass
class FakeHyperliquid:
    """In-process Hyperliquid stand-in. No network, no keys, no withdraw."""

    mids: dict[str, float] = field(
        default_factory=lambda: {"BTC": 65000.0, "ETH": 3500.0}
    )
    candles: list[dict[str, Any]] = field(default_factory=list)
    user_states: dict[str, dict[str, Any]] = field(default_factory=dict)
    orders: list[dict[str, Any]] = field(default_factory=list)

    @classmethod
    def with_synthetic_candles(
        cls,
        n: int = 500,
        *,
        coin: str = "BTC",
        start: float = 30_000.0,
        seed: int = 7,
    ) -> "FakeHyperliquid":
        fake = cls()
        fake.seed_daily_closes(n, start=start, coin=coin, seed=seed)
        return fake

    def seed_daily_closes(
        self,
        n: int = 500,
        *,
        start: float = 30_000.0,
        coin: str = "BTC",
        seed: int = 7,
    ) -> None:
        closes = synthetic_ohlc(n, s0=start, seed=seed)
        now_ms = int(time.time() * 1000)
        self.candles = [
            {
                "t": now_ms - (n - i) * 86_400_000,
                "T": now_ms - (n - i) * 86_400_000 + 86_399_999,
                "s": coin,
                "i": "1d",
                "o": str(float(closes[i])),
                "c": str(float(closes[i])),
                "h": str(float(closes[i])),
                "l": str(float(closes[i])),
                "v": "0",
                "n": 1,
            }
            for i in range(n)
        ]
        self.mids[coin] = float(closes[-1])

    def all_mids(self) -> dict[str, float]:
        return {coin: float(px) for coin, px in self.mids.items()}

    def user_state(self, address: str) -> dict[str, Any]:
        key = address.lower()
        if key in self.user_states:
            return dict(self.user_states[key])
        return {
            "marginSummary": {"accountValue": "0", "totalNtlPos": "0"},
            "assetPositions": [],
        }

    def candle_snapshot(
        self, coin: str, interval: str, start_ms: int, end_ms: int
    ) -> list[dict[str, Any]]:
        rows = list(self.candles)
        if rows:
            return [
                row
                for row in rows
                if (not coin or row.get("s") in {coin, None})
                and start_ms <= int(row.get("t") or 0) <= end_ms
            ] or rows
        self.seed_daily_closes(coin=coin)
        return list(self.candles)

    def market_order(
        self,
        coin: str,
        is_buy: bool,
        size: float,
        *,
        reduce_only: bool = False,
    ) -> dict[str, Any]:
        mid = float(self.mids.get(coin) or 0.0)
        fill = {
            "ok": True,
            "status": "ok",
            "coin": coin,
            "is_buy": is_buy,
            "sz": float(size),
            "px": mid,
            "reduce_only": reduce_only,
        }
        self.orders.append(dict(fill))
        return fill

    def handle_info(self, payload: dict[str, Any]) -> Any:
        kind = payload.get("type")
        if kind == "allMids":
            return {coin: str(px) for coin, px in self.all_mids().items()}
        if kind == "clearinghouseState":
            return self.user_state(str(payload.get("user") or ""))
        if kind == "candleSnapshot":
            req = payload.get("req") or {}
            return self.candle_snapshot(
                str(req.get("coin") or "BTC"),
                str(req.get("interval") or "1d"),
                int(req.get("startTime") or 0),
                int(req.get("endTime") or int(time.time() * 1000)),
            )
        raise HyperliquidError(f"unsupported info type {kind!r}")


class HyperliquidClient:
    """Info over HTTP; signed orders via fake or the official SDK.

    The ETH key never appears in snapshots, repr, or returned dicts.
    Withdraw / transfer / vault methods are intentionally absent and
    the explicit stubs raise.
    """

    def __init__(
        self,
        *,
        testnet: bool = True,
        eth_key: str | None = None,
        fake: FakeHyperliquid | None = None,
        slippage: float = 0.01,
        info_url: str | None = None,
        timeout_s: float = 20.0,
    ) -> None:
        self.testnet = bool(testnet)
        self.fake = fake
        self.slippage = float(slippage)
        self.info_url = info_url or default_info_url(testnet=self.testnet)
        self.timeout_s = timeout_s
        self._eth_key = eth_key

    def __repr__(self) -> str:
        return (
            f"HyperliquidClient(testnet={self.testnet}, "
            f"fake={self.fake is not None})"
        )

    def all_mids(self) -> dict[str, float]:
        raw = self._post_info({"type": "allMids"})
        if not isinstance(raw, dict):
            raise HyperliquidError("allMids: expected an object")
        out: dict[str, float] = {}
        for coin, value in raw.items():
            try:
                px = float(value)
            except (TypeError, ValueError) as exc:
                raise HyperliquidError(f"allMids: bad mid for {coin}") from exc
            if px > 0:
                out[str(coin)] = px
        return out

    def user_state(self, address: str) -> dict[str, Any]:
        raw = self._post_info({"type": "clearinghouseState", "user": address})
        if not isinstance(raw, dict):
            raise HyperliquidError("clearinghouseState: expected an object")
        return raw

    def candles(
        self, coin: str, interval: str, start_ms: int, end_ms: int
    ) -> list[dict[str, Any]]:
        raw = self._post_info(
            {
                "type": "candleSnapshot",
                "req": {
                    "coin": coin,
                    "interval": interval,
                    "startTime": int(start_ms),
                    "endTime": int(end_ms),
                },
            }
        )
        if not isinstance(raw, list):
            raise HyperliquidError("candleSnapshot: expected a list")
        return raw

    def market_order(
        self,
        coin: str,
        is_buy: bool,
        size: float,
        *,
        reduce_only: bool = False,
    ) -> dict[str, Any]:
        if self.fake is not None:
            return self.fake.market_order(
                coin, is_buy, size, reduce_only=reduce_only
            )
        return self._sdk_market_order(coin, is_buy, size, reduce_only=reduce_only)

    def withdraw(self, *_args: Any, **_kwargs: Any) -> None:
        raise HyperliquidError("Hyperliquid withdrawals are disabled")

    def usd_transfer(self, *_args: Any, **_kwargs: Any) -> None:
        raise HyperliquidError("Hyperliquid transfers are disabled")

    def vault_transfer(self, *_args: Any, **_kwargs: Any) -> None:
        raise HyperliquidError("Hyperliquid vault transfers are disabled")

    def _post_info(self, payload: dict[str, Any]) -> Any:
        if self.fake is not None:
            return self.fake.handle_info(payload)
        with httpx.Client(timeout=self.timeout_s) as client:
            response = client.post(self.info_url, json=payload)
            response.raise_for_status()
            return response.json()

    def _sdk_market_order(
        self,
        coin: str,
        is_buy: bool,
        size: float,
        *,
        reduce_only: bool,
    ) -> dict[str, Any]:
        if not self._eth_key:
            return {"ok": False, "reason": "missing_signer"}
        try:
            from eth_account import Account
            from hyperliquid.exchange import Exchange
            from hyperliquid.utils import constants
        except ImportError:
            return {
                "ok": False,
                "reason": "sdk_missing",
                "hint": "pip install 'sovereign[hyperliquid]'",
            }
        account = Account.from_key(self._eth_key)
        base = constants.TESTNET_API_URL if self.testnet else constants.MAINNET_API_URL
        exchange = Exchange(account, base)
        if reduce_only:
            raw = exchange.market_close(coin)
        else:
            raw = exchange.market_open(coin, is_buy, float(size), None, self.slippage)
        mid = 0.0
        try:
            mid = float((self.all_mids() or {}).get(coin) or 0.0)
        except Exception:
            mid = 0.0
        status = ""
        if isinstance(raw, dict):
            status = str(raw.get("status") or "")
        ok = status in {"ok", "success"} or (
            isinstance(raw, dict) and raw.get("ok") is True
        )
        return {
            "ok": bool(ok),
            "status": status or ("ok" if ok else "error"),
            "coin": coin,
            "is_buy": is_buy,
            "sz": float(size),
            "px": mid,
            "reduce_only": reduce_only,
            "raw_status": status,
        }


@dataclass
class HyperliquidBroker(PaperBroker):
    """Paper accounting + optional live Hyperliquid market orders."""

    client: HyperliquidClient | None = None
    coin: str = "BTC"
    slippage: float = 0.01
    min_order_usd: float = 12.0
    last_mid: float = 0.0

    def __post_init__(self) -> None:
        self.venue = "hyperliquid"

    def snapshot(self) -> dict[str, Any]:
        snap = super().snapshot()
        snap.update(
            {
                "coin": self.coin,
                "last_mid": self.last_mid,
                "live": self.client is not None,
            }
        )
        return snap

    def restore(self, snap: dict[str, Any], *, now=None) -> None:
        super().restore(snap, now=now)
        if snap.get("coin"):
            self.coin = str(snap["coin"])
        if "last_mid" in snap:
            try:
                self.last_mid = float(snap.get("last_mid") or 0.0)
            except (TypeError, ValueError):
                self.last_mid = 0.0

    def target_position(
        self, desired_notional: float, price: float, cost: float
    ) -> dict[str, Any]:
        if self.client is None:
            return super().target_position(desired_notional, price, cost)
        if self.frozen:
            return {"ok": False, "reason": "frozen_or_bad_price"}
        try:
            mids = self.client.all_mids()
            mid = float(mids.get(self.coin) or 0.0)
        except Exception as exc:
            return {"ok": False, "reason": f"mid_unavailable: {exc}"}
        if mid > 0:
            price = mid
            self.last_mid = mid
            self.mark(mid)
        if price <= 0:
            return {"ok": False, "reason": "frozen_or_bad_price"}
        desired_qty = desired_notional / price
        delta = desired_qty - self.position
        notional = abs(delta) * price
        if notional < self.min_order_usd:
            return {
                "ok": False,
                "reason": "below_min_order",
                "min_order_usd": self.min_order_usd,
                "notional": notional,
            }
        placed = self.client.market_order(
            self.coin, delta > 0, abs(delta), reduce_only=False
        )
        if not placed.get("ok"):
            return {"ok": False, "reason": "order_rejected", "exchange": placed}
        fill_px = float(placed.get("px") or price)
        if fill_px <= 0:
            fill_px = price
        fill = super().target_position(desired_notional, fill_px, cost)
        fill["venue"] = "hyperliquid"
        fill["coin"] = self.coin
        return fill


def build_info_client(config: EngineConfig) -> HyperliquidClient:
    trading = config.trading
    fake = FakeHyperliquid.with_synthetic_candles(coin=trading.coin) if _use_fake(trading) else None
    return HyperliquidClient(testnet=trading.hyperliquid_testnet, fake=fake)


def build_broker(config: EngineConfig, wallet: Any | None = None) -> PaperBroker:
    """Sim and paper venue stay on :class:`PaperBroker`.

    Live Hyperliquid without ``hyperliquid_enabled`` still returns a
    Hyperliquid-named paper book so the trader can skip fail-closed.
    """
    trading = config.trading
    if config.mode != "live" or trading.venue == "paper":
        return PaperBroker(cash=0.0, venue="paper")
    if trading.venue != "hyperliquid":
        return PaperBroker(cash=0.0, venue="paper")
    if not trading.hyperliquid_enabled:
        return PaperBroker(cash=0.0, venue="hyperliquid")
    if not trading.hyperliquid_testnet and not trading.hyperliquid_allow_mainnet:
        raise HyperliquidError(
            "Hyperliquid mainnet requires trading.hyperliquid_allow_mainnet"
        )
    fake = (
        FakeHyperliquid.with_synthetic_candles(coin=trading.coin)
        if _use_fake(trading)
        else None
    )
    eth_key = None
    if wallet is not None and fake is None:
        eth_key = wallet.load_or_create().eth_key
    client = HyperliquidClient(
        testnet=trading.hyperliquid_testnet,
        eth_key=eth_key,
        fake=fake,
        slippage=trading.slippage,
    )
    return HyperliquidBroker(
        cash=0.0,
        client=client,
        coin=trading.coin,
        slippage=trading.slippage,
        min_order_usd=trading.min_order_usd,
    )


def fetch_hyperliquid_closes(
    coin: str = "BTC",
    *,
    client: HyperliquidClient | None = None,
    testnet: bool = True,
    bars: int = 1000,
) -> np.ndarray:
    http = client or HyperliquidClient(testnet=testnet)
    end_ms = int(time.time() * 1000)
    start_ms = end_ms - max(bars, 1) * 86_400_000
    rows = http.candles(coin, "1d", start_ms, end_ms)
    try:
        closes = [row["c"] for row in rows]
        timestamps = [row["t"] for row in rows]
    except (KeyError, TypeError) as exc:
        raise ValueError("hyperliquid: malformed candle row") from exc
    return validate_closes(closes, timestamps=timestamps, source="hyperliquid")
