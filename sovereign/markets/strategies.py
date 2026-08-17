from __future__ import annotations

from dataclasses import dataclass

import numpy as np

from sovereign.markets.stats import execute, metrics_from_returns, returns_from_close, rolling_mean, rolling_std


@dataclass
class Strategy:
    id: str
    name: str
    description: str

    def positions(self, close: np.ndarray) -> np.ndarray:
        raise NotImplementedError


class TimeSeriesMomentum(Strategy):
    """Moskowitz/Ooi/Pedersen-style TSMOM with volatility targeting.

    Long when price is above slow SMA, flat/short when below. Leverage is
    scaled so realized vol approximates target_vol. This is the primary
    candidate for certification — not a promise of future profit.
    """

    def __init__(
        self,
        lookback: int = 50,
        vol_lookback: int = 20,
        target_vol: float = 0.20,
        periods_per_year: int = 365,
        allow_short: bool = False,
    ) -> None:
        super().__init__(
            id="tsmom_vol",
            name="Time-series momentum + vol target",
            description="Trend follow BTC/ETH, size by inverse vol, flatten in chaos.",
        )
        self.lookback = lookback
        self.vol_lookback = vol_lookback
        self.target_vol = target_vol
        self.periods_per_year = periods_per_year
        self.allow_short = allow_short

    def positions(self, close: np.ndarray) -> np.ndarray:
        close = close.astype(float)
        sma = rolling_mean(close, self.lookback)
        logret = np.diff(np.log(np.maximum(close, 1e-12)), prepend=np.nan)
        vol = rolling_std(logret, self.vol_lookback) * np.sqrt(self.periods_per_year)
        signal = np.where(close > sma, 1.0, -1.0 if self.allow_short else 0.0)
        lev = np.clip(self.target_vol / (vol + 1e-8), 0.0, 2.0)
        pos = signal * lev
        warmup = max(self.lookback, self.vol_lookback)
        pos[:warmup] = 0.0
        pos[~np.isfinite(pos)] = 0.0
        return pos


class DualMA(Strategy):
    def __init__(self, fast: int = 50, slow: int = 200) -> None:
        super().__init__(
            id="dual_ma",
            name="Dual moving-average regime",
            description="Risk-on only when fast SMA > slow SMA. Capital preservation.",
        )
        self.fast = fast
        self.slow = slow

    def positions(self, close: np.ndarray) -> np.ndarray:
        f = rolling_mean(close.astype(float), self.fast)
        s = rolling_mean(close.astype(float), self.slow)
        pos = np.where(f > s, 1.0, 0.0)
        pos[: self.slow] = 0.0
        pos[~np.isfinite(pos)] = 0.0
        return pos


class ZScoreMeanReversion(Strategy):
    """Often fails certification on trending crypto. Kept so the gate can reject it."""

    def __init__(self, lookback: int = 20, entry: float = 1.5) -> None:
        super().__init__(
            id="zscore_mr",
            name="Z-score mean reversion",
            description="Fade stretched 20-day returns. Hostile to strong trends.",
        )
        self.lookback = lookback
        self.entry = entry

    def positions(self, close: np.ndarray) -> np.ndarray:
        close = close.astype(float)
        logc = np.log(np.maximum(close, 1e-12))
        r = np.zeros_like(logc)
        r[1:] = np.diff(logc)
        mu = rolling_mean(r, self.lookback)
        sd = rolling_std(r, self.lookback)
        z = (r - mu) / (sd + 1e-12)
        pos = np.where(z > self.entry, -1.0, np.where(z < -self.entry, 1.0, 0.0))
        pos[: self.lookback] = 0.0
        pos[~np.isfinite(pos)] = 0.0
        return pos


def backtest(strategy: Strategy, close: np.ndarray, cost: float = 0.001) -> dict:
    close = np.asarray(close, dtype=float)
    ret = returns_from_close(close)
    pos = strategy.positions(close)
    net, held = execute(pos, ret, cost, lag=1)
    m = metrics_from_returns(np.nan_to_num(net, nan=0.0), position=held)
    return {
        "strategy_id": strategy.id,
        "name": strategy.name,
        "metrics": m.as_dict(),
        "last_position": float(held[-1]) if len(held) else 0.0,
        "equity_end": round(float(np.cumprod(1.0 + np.nan_to_num(net, nan=0.0))[-1]), 4)
        if len(net)
        else 1.0,
    }


STRATEGIES: dict[str, Strategy] = {
    "tsmom_vol": TimeSeriesMomentum(),
    "dual_ma": DualMA(),
    "zscore_mr": ZScoreMeanReversion(),
}
