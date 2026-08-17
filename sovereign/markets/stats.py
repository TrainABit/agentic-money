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
    for i in range(n - 1, len(x)):
        out[i] = np.std(x[i - n + 1 : i + 1], ddof=1)
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
    hit_rate: float
    calmar: float

    def as_dict(self) -> dict[str, float | int]:
        return {
            "total_return": round(self.total_return, 4),
            "sharpe": round(self.sharpe, 4),
            "max_drawdown": round(self.max_drawdown, 4),
            "n_trades": int(self.n_trades),
            "hit_rate": round(self.hit_rate, 4),
            "calmar": round(self.calmar, 4),
        }


def returns_from_close(close: np.ndarray) -> np.ndarray:
    close = np.asarray(close, dtype=float)
    ret = np.zeros_like(close)
    if len(close) > 1:
        ret[1:] = np.diff(close) / np.maximum(close[:-1], 1e-12)
    return ret


def execute(position: np.ndarray, ret: np.ndarray, cost: float, lag: int = 1) -> tuple[np.ndarray, np.ndarray]:
    """Hold the signal from bar t during bar t+lag. Default lag=1 removes same-bar look-ahead."""
    position = np.asarray(position, dtype=float)
    ret = np.asarray(ret, dtype=float)
    held = np.zeros_like(position)
    if lag <= 0:
        held = position.copy()
    elif len(position) > lag:
        held[lag:] = position[:-lag]
    turnover = np.abs(np.diff(held, prepend=0.0))
    net = held * ret - turnover * cost
    return net, held


def apply_costs(position: np.ndarray, ret: np.ndarray, cost: float) -> np.ndarray:
    net, _ = execute(position, ret, cost, lag=1)
    return net


def metrics_from_returns(
    rets: np.ndarray,
    periods_per_year: int = 365,
    position: np.ndarray | None = None,
) -> Metrics:
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
        n_trades = int(np.sum(np.abs(np.diff(p, prepend=0.0)) > 1e-12))
    else:
        n_trades = 0
    hit = float(np.mean(rets > 0))
    calmar = (float(equity[-1] - 1.0) / dd) if dd > 1e-9 else 0.0
    return Metrics(float(equity[-1] - 1.0), float(sharpe), float(dd), n_trades, hit, float(calmar))
