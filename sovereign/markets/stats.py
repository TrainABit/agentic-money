from __future__ import annotations

from dataclasses import dataclass

import numpy as np


def rolling_mean(x: np.ndarray, n: int) -> np.ndarray:
    out = np.full_like(x, np.nan, dtype=float)
    if n <= 0 or len(x) < n:
        return out
    c = np.cumsum(np.insert(x.astype(float), 0, 0.0))
    out[n - 1 :] = (c[n:] - c[:-n]) / n
    return out


def rolling_std(x: np.ndarray, n: int) -> np.ndarray:
    out = np.full_like(x, np.nan, dtype=float)
    if n <= 0 or len(x) < n:
        return out
    windows = np.lib.stride_tricks.sliding_window_view(np.asarray(x, dtype=float), n)
    with np.errstate(divide="ignore", invalid="ignore"):
        out[n - 1 :] = np.std(windows, axis=-1, ddof=1)
    return out


def max_drawdown(equity: np.ndarray) -> float:
    peak = np.maximum.accumulate(equity)
    dd = (equity - peak) / np.maximum(peak, 1e-12)
    return float(dd.min()) if len(dd) else 0.0


@dataclass
class Metrics:
    total_return: float
    sharpe: float
    max_drawdown: float
    n_trades: int
    positive_bar_rate: float
    calmar: float

    def as_dict(self) -> dict[str, float | int]:
        return {
            "total_return": round(self.total_return, 4),
            "sharpe": round(self.sharpe, 4),
            "max_drawdown": round(self.max_drawdown, 4),
            "n_trades": int(self.n_trades),
            "positive_bar_rate": round(self.positive_bar_rate, 4),
            "calmar": round(self.calmar, 4),
        }


def returns_from_close(close: np.ndarray) -> np.ndarray:
    close = np.asarray(close, dtype=float)
    ret = np.zeros_like(close)
    if len(close) > 1:
        ret[1:] = np.diff(close) / np.maximum(close[:-1], 1e-12)
    return ret


def execute(
    position: np.ndarray,
    ret: np.ndarray,
    round_trip_cost: float = 0.0,
    lag: int = 1,
    *,
    cost: float | None = None,
) -> tuple[np.ndarray, np.ndarray]:
    """Execute lagged positions and charge a full round-trip cost per unit cycle.

    A unit entry or exit is one side of a round trip and is charged half of
    ``round_trip_cost``. A direct long-to-short flip has two units of turnover
    and therefore incurs one full round-trip cost. ``cost`` is retained as a
    backwards-compatible keyword alias.
    """
    if cost is not None:
        round_trip_cost = cost
    if not np.isfinite(round_trip_cost) or round_trip_cost < 0:
        raise ValueError("round_trip_cost must be finite and non-negative")
    position = np.asarray(position, dtype=float)
    ret = np.asarray(ret, dtype=float)
    if position.shape != ret.shape:
        raise ValueError("position and return arrays must have the same shape")
    held = np.zeros_like(position)
    if lag <= 0:
        held = position.copy()
    elif len(position) > lag:
        held[lag:] = position[:-lag]
    turnover = np.abs(np.diff(held, prepend=0.0))
    net = held * ret - turnover * (round_trip_cost / 2.0)
    return net, held


def apply_costs(
    position: np.ndarray,
    ret: np.ndarray,
    round_trip_cost: float = 0.0,
    *,
    cost: float | None = None,
) -> np.ndarray:
    """Apply lag and round-trip transaction-cost semantics to a return series."""
    if cost is not None:
        round_trip_cost = cost
    net, _ = execute(position, ret, round_trip_cost, lag=1)
    return net


def metrics_from_returns(
    rets: np.ndarray,
    periods_per_year: int = 365,
    position: np.ndarray | None = None,
) -> Metrics:
    """Summarize bar returns and economic position-state transitions.

    ``n_trades`` counts entries, exits, and direction changes. Changes in
    position magnitude while remaining long or short (for example volatility
    target rebalancing) are not additional trades. ``positive_bar_rate`` is
    the fraction of evaluated bars with a strictly positive net return; it is
    not a round-trip trade win rate.
    """
    rets = np.asarray(rets, dtype=float)
    rets = rets[np.isfinite(rets)]
    if len(rets) < 2:
        return Metrics(0, 0, 0, 0, 0, 0)
    equity = np.cumprod(1.0 + rets)
    mu = float(np.mean(rets))
    sd = float(np.std(rets, ddof=1)) or 1e-12
    sharpe = mu / sd * np.sqrt(periods_per_year)
    dd = abs(max_drawdown(equity))
    if position is not None:
        p = np.asarray(position, dtype=float)
        economic_state = np.zeros_like(p, dtype=np.int8)
        economic_state[p > 1e-12] = 1
        economic_state[p < -1e-12] = -1
        n_trades = int(np.count_nonzero(np.diff(economic_state, prepend=0)))
    else:
        n_trades = 0
    positive_bar_rate = float(np.mean(rets > 0))
    calmar = (float(equity[-1] - 1.0) / dd) if dd > 1e-9 else 0.0
    return Metrics(
        float(equity[-1] - 1.0),
        float(sharpe),
        float(dd),
        n_trades,
        positive_bar_rate,
        float(calmar),
    )
