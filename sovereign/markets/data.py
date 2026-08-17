from __future__ import annotations

from typing import Any

import httpx
import numpy as np

from sovereign.config import RiskLimits
from sovereign.markets.stats import execute, metrics_from_returns, returns_from_close
from sovereign.markets.strategies import STRATEGIES, Strategy, backtest

TRAIN_BARS = 400
TEST_BARS = 80
MIN_CERTIFICATION_BARS = TRAIN_BARS + TEST_BARS


def validate_closes(
    values: Any,
    *,
    timestamps: Any | None = None,
    minimum: int = MIN_CERTIFICATION_BARS,
    source: str = "market data",
) -> np.ndarray:
    """Return a validated one-dimensional close series.

    Fetched prices must be finite, strictly positive, long enough to certify,
    and strictly chronological whenever their source supplies timestamps.
    """
    try:
        closes = np.asarray(values, dtype=float)
    except (TypeError, ValueError) as exc:
        raise ValueError(f"{source}: closes are not numeric") from exc
    if closes.ndim != 1:
        raise ValueError(f"{source}: closes must be one-dimensional")
    if len(closes) < minimum:
        raise ValueError(f"{source}: too short ({len(closes)} bars; need at least {minimum})")
    if not np.all(np.isfinite(closes)):
        raise ValueError(f"{source}: closes contain non-finite values")
    if np.any(closes <= 0):
        raise ValueError(f"{source}: closes must be strictly positive")

    if timestamps is not None:
        try:
            ordered_at = np.asarray(timestamps, dtype=float)
        except (TypeError, ValueError) as exc:
            raise ValueError(f"{source}: timestamps are not numeric") from exc
        if ordered_at.ndim != 1 or len(ordered_at) != len(closes):
            raise ValueError(f"{source}: timestamps must align one-to-one with closes")
        if not np.all(np.isfinite(ordered_at)):
            raise ValueError(f"{source}: timestamps contain non-finite values")
        if len(ordered_at) > 1 and np.any(np.diff(ordered_at) <= 0):
            raise ValueError(f"{source}: timestamps are not strictly increasing")
    return closes


def synthetic_ohlc(
    n: int = 800,
    s0: float = 30000.0,
    mu: float = 0.0007,
    sigma: float = 0.03,
    seed: int = 7,
) -> np.ndarray:
    rng = np.random.default_rng(seed)
    r = rng.normal(mu, sigma, n)
    # Mild positive drift + occasional trend regimes so TSMOM has something to hunt
    regime = np.sin(np.linspace(0, 8 * np.pi, n)) * 0.0015
    close = s0 * np.exp(np.cumsum(r + regime))
    return close


def fetch_binance_closes(symbol: str = "BTCUSDT", interval: str = "1d", limit: int = 1000) -> np.ndarray:
    url = "https://api.binance.com/api/v3/klines"
    with httpx.Client(timeout=20.0) as client:
        r = client.get(url, params={"symbol": symbol, "interval": interval, "limit": limit})
        r.raise_for_status()
        data = r.json()
    if not isinstance(data, list):
        raise TypeError("binance: expected a list of klines")
    try:
        closes = [row[4] for row in data]
        timestamps = [row[0] for row in data]
    except (IndexError, TypeError) as exc:
        raise ValueError("binance: malformed kline row") from exc
    return validate_closes(closes, timestamps=timestamps, source="binance")


def fetch_kraken_closes() -> np.ndarray:
    url = "https://api.kraken.com/0/public/OHLC"
    with httpx.Client(timeout=20.0) as client:
        r = client.get(url, params={"pair": "XBTUSD", "interval": 1440})
        r.raise_for_status()
        data = r.json()
    if not isinstance(data, dict):
        raise TypeError("kraken: expected an object response")
    if data.get("error"):
        raise ValueError(f"kraken: API errors: {data['error']}")
    result = data.get("result") or {}
    series = next((v for k, v in result.items() if k != "last"), None)
    if not isinstance(series, list):
        raise TypeError("kraken: OHLC series missing")
    try:
        closes = [row[4] for row in series]
        timestamps = [row[0] for row in series]
    except (IndexError, TypeError) as exc:
        raise ValueError("kraken: malformed OHLC row") from exc
    return validate_closes(closes, timestamps=timestamps, source="kraken")


def fetch_yahoo_closes(symbol: str = "BTC-USD") -> np.ndarray:
    url = f"https://query1.finance.yahoo.com/v8/finance/chart/{symbol}"
    headers = {"User-Agent": "SovereignEngine/0.1"}
    with httpx.Client(timeout=20.0, headers=headers) as client:
        r = client.get(url, params={"interval": "1d", "range": "2y"})
        r.raise_for_status()
        data = r.json()
    try:
        result = data["chart"]["result"][0]
        timestamps = result["timestamp"]
        closes = result["indicators"]["quote"][0]["close"]
    except (IndexError, KeyError, TypeError) as exc:
        raise ValueError("yahoo: malformed chart response") from exc
    if len(timestamps) != len(closes):
        raise ValueError("yahoo: timestamps do not align with closes")
    pairs = [(ts, close) for ts, close in zip(timestamps, closes, strict=True) if close is not None]
    return validate_closes(
        [close for _, close in pairs],
        timestamps=[ts for ts, _ in pairs],
        source="yahoo",
    )


def fetch_closes() -> tuple[np.ndarray, str]:
    errors: list[str] = []
    for name, fn in (
        ("binance", fetch_binance_closes),
        ("kraken", fetch_kraken_closes),
        ("yahoo", fetch_yahoo_closes),
    ):
        try:
            arr = validate_closes(fn(), source=name)
            return arr, name
        except Exception as e:  # noqa: BLE001 - continue through independent sources
            errors.append(f"{name}: {type(e).__name__}: {e}")
    raise RuntimeError("all market sources failed: " + " | ".join(errors))


def walk_forward(
    strategy: Strategy,
    close: np.ndarray,
    train: int = TRAIN_BARS,
    test: int = TEST_BARS,
    round_trip_cost: float = 0.001,
    *,
    cost: float | None = None,
) -> dict[str, Any]:
    """Rolling train/test. Certification uses OOS concatenations only."""
    if cost is not None:
        round_trip_cost = cost
    if train <= 0 or test <= 0:
        raise ValueError("train and test windows must be positive")
    close = validate_closes(close, minimum=0, source="certification data")
    oos_rets = []
    oos_pos: list[np.ndarray] = []
    windows = 0
    i = 0
    n = len(close)
    required = train + test
    if n < required:
        return {
            "windows": 0,
            "oos": metrics_from_returns(np.array([], dtype=float)).as_dict(),
            "method": "insufficient_data",
            "status": "insufficient_data",
            "insufficient_data": {
                "available_bars": n,
                "required_bars": required,
                "train_bars": train,
                "test_bars": test,
            },
        }
    while i + train + test <= n:
        sl = close[i : i + train + test]
        ret = returns_from_close(sl)
        pos = strategy.positions(sl)
        net, held = execute(pos, ret, round_trip_cost, lag=1)
        oos_rets.append(np.nan_to_num(net[train : train + test], nan=0.0))
        oos_pos.append(held[train : train + test])
        windows += 1
        i += test
    cat = np.concatenate(oos_rets)
    held_cat = np.concatenate(oos_pos)
    m = metrics_from_returns(cat, position=held_cat)
    return {
        "windows": windows,
        "oos": m.as_dict(),
        "method": "walk_forward",
        "status": "ok",
        "insufficient_data": None,
    }


def certify(close: np.ndarray, limits: RiskLimits) -> list[dict[str, Any]]:
    close = validate_closes(close, minimum=0, source="certification data")
    reports = []
    for sid, strat in STRATEGIES.items():
        is_report = backtest(strat, close, round_trip_cost=limits.round_trip_cost)
        oos = walk_forward(strat, close, round_trip_cost=limits.round_trip_cost)
        oos_m = oos["oos"]
        insufficient_data = oos["insufficient_data"]
        passed = bool(
            insufficient_data is None
            and oos_m["sharpe"] >= limits.min_sharpe_oos
            and abs(oos_m["max_drawdown"]) <= limits.max_drawdown_oos
            and oos_m["n_trades"] >= limits.min_trades_oos
        )
        if insufficient_data is not None:
            reason = (
                f"insufficient_data: {insufficient_data['available_bars']} bars available; "
                f"{insufficient_data['required_bars']} required "
                f"(train={insufficient_data['train_bars']}, test={insufficient_data['test_bars']})"
            )
        elif passed:
            reason = "passes walk-forward gates"
        else:
            reason = (
                f"reject: sharpe={oos_m['sharpe']}, dd={oos_m['max_drawdown']}, "
                f"trades={oos_m['n_trades']}"
            )
        reports.append(
            {
                "strategy_id": sid,
                "name": strat.name,
                "in_sample": is_report["metrics"],
                "oos": oos_m,
                "oos_method": oos["method"],
                "oos_windows": oos["windows"],
                "certified": bool(passed),
                "insufficient_data": insufficient_data,
                "reason": reason,
            }
        )
    return reports
