from __future__ import annotations

from typing import Any

import httpx
import numpy as np

from sovereign.config import RiskLimits
from sovereign.markets.strategies import STRATEGIES, Strategy, backtest


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
    return np.array([float(row[4]) for row in data], dtype=float)


def fetch_kraken_closes() -> np.ndarray:
    url = "https://api.kraken.com/0/public/OHLC"
    with httpx.Client(timeout=20.0) as client:
        r = client.get(url, params={"pair": "XBTUSD", "interval": 1440})
        r.raise_for_status()
        data = r.json()
    result = data.get("result") or {}
    series = next(v for k, v in result.items() if k != "last")
    return np.array([float(row[4]) for row in series], dtype=float)


def fetch_yahoo_closes(symbol: str = "BTC-USD") -> np.ndarray:
    url = f"https://query1.finance.yahoo.com/v8/finance/chart/{symbol}"
    headers = {"User-Agent": "SovereignEngine/0.1"}
    with httpx.Client(timeout=20.0, headers=headers) as client:
        r = client.get(url, params={"interval": "1d", "range": "2y"})
        r.raise_for_status()
        data = r.json()
    closes = data["chart"]["result"][0]["indicators"]["quote"][0]["close"]
    return np.array([float(x) for x in closes if x is not None], dtype=float)


def fetch_closes() -> tuple[np.ndarray, str]:
    errors: list[str] = []
    for name, fn in (
        ("binance", fetch_binance_closes),
        ("kraken", fetch_kraken_closes),
        ("yahoo", fetch_yahoo_closes),
    ):
        try:
            arr = fn()
            if len(arr) >= 100:
                return arr, name
            errors.append(f"{name}: too short ({len(arr)})")
        except Exception as e:
            errors.append(f"{name}: {e}")
    return synthetic_ohlc(), "synthetic:" + " | ".join(errors)


def walk_forward(
    strategy: Strategy,
    close: np.ndarray,
    train: int = 400,
    test: int = 80,
    cost: float = 0.001,
) -> dict[str, Any]:
    """Rolling train/test. Certification uses OOS concatenations only."""
    oos_rets = []
    windows = 0
    i = 0
    n = len(close)
    while i + train + test <= n:
        sl = close[i : i + train + test]
        # Positions computed on the window; take only the test segment returns
        full = backtest(strategy, sl, cost=cost)
        # Recompute OOS piece
        ret = np.diff(sl, prepend=sl[0]) / np.maximum(sl, 1e-12)
        pos = strategy.positions(sl)
        from sovereign.markets.stats import apply_costs, metrics_from_returns

        net = apply_costs(pos, ret, cost)
        oos = net[train : train + test]
        oos_rets.append(np.nan_to_num(oos, nan=0.0))
        windows += 1
        i += test
        _ = full
    if not oos_rets:
        # Short series: treat second half as OOS
        split = max(len(close) // 2, 10)
        ret = np.diff(close, prepend=close[0]) / np.maximum(close, 1e-12)
        pos = strategy.positions(close)
        from sovereign.markets.stats import apply_costs, metrics_from_returns

        net = apply_costs(pos, ret, cost)
        m = metrics_from_returns(np.nan_to_num(net[split:], nan=0.0))
        return {"windows": 1, "oos": m.as_dict(), "method": "half_split"}
    from sovereign.markets.stats import metrics_from_returns

    cat = np.concatenate(oos_rets)
    m = metrics_from_returns(cat)
    return {"windows": windows, "oos": m.as_dict(), "method": "walk_forward"}


def certify(close: np.ndarray, limits: RiskLimits) -> list[dict[str, Any]]:
    reports = []
    for sid, strat in STRATEGIES.items():
        is_report = backtest(strat, close, cost=limits.round_trip_cost)
        oos = walk_forward(strat, close, cost=limits.round_trip_cost)
        oos_m = oos["oos"]
        passed = (
            oos_m["sharpe"] >= limits.min_sharpe_oos
            and abs(oos_m["max_drawdown"]) <= limits.max_drawdown_oos
            and oos_m["n_trades"] >= limits.min_trades_oos
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
                "reason": (
                    "passes walk-forward gates"
                    if passed
                    else (
                        f"reject: sharpe={oos_m['sharpe']}, dd={oos_m['max_drawdown']}, "
                        f"trades={oos_m['n_trades']}"
                    )
                ),
            }
        )
    return reports
