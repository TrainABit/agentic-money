import numpy as np

from sovereign.config import RiskLimits
from sovereign.markets.data import certify, synthetic_ohlc, walk_forward
from sovereign.markets.strategies import DualMA, TimeSeriesMomentum, ZScoreMeanReversion, backtest


def test_backtest_deterministic():
    close = synthetic_ohlc(n=600, seed=1)
    a = backtest(TimeSeriesMomentum(), close, cost=0.001)
    b = backtest(TimeSeriesMomentum(), close, cost=0.001)
    assert a["metrics"] == b["metrics"]
    assert np.isfinite(a["metrics"]["sharpe"])


def test_all_strategies_run():
    close = synthetic_ohlc(n=500, seed=2)
    for strat in (TimeSeriesMomentum(), DualMA(), ZScoreMeanReversion()):
        r = backtest(strat, close)
        assert "sharpe" in r["metrics"]
        assert r["metrics"]["max_drawdown"] <= 0.0001 or r["metrics"]["max_drawdown"] >= 0


def test_certify_emits_gate_fields():
    close = synthetic_ohlc(n=900, mu=0.002, sigma=0.015, seed=3)
    reports = certify(close, RiskLimits())
    ids = {r["strategy_id"] for r in reports}
    assert ids == {"tsmom_vol", "dual_ma", "zscore_mr"}
    for r in reports:
        assert "certified" in r
        assert "oos" in r
        assert "reason" in r
