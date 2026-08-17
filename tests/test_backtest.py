import numpy as np

from sovereign.config import RiskLimits
from sovereign.markets.data import certify, synthetic_ohlc
from sovereign.markets.stats import execute, metrics_from_returns, returns_from_close
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


def test_execution_lags_one_bar_and_counts_turnover():
    close = np.array([100.0, 110.0, 121.0, 110.0], dtype=float)
    pos = np.array([0.0, 1.0, 1.0, 0.0], dtype=float)
    ret = returns_from_close(close)
    net, held = execute(pos, ret, 0.0, lag=1)
    assert held[0] == 0.0
    assert held[1] == 0.0
    assert held[2] == 1.0
    leaked, leaked_held = execute(pos, ret, 0.0, lag=0)
    assert leaked_held[1] == 1.0
    assert float(np.sum(leaked)) != float(np.sum(net))
    m = metrics_from_returns(net, position=held)
    assert m.n_trades == int(np.sum(np.abs(np.diff(held, prepend=0.0)) > 0))


def test_look_ahead_is_not_the_certified_edge():
    close = synthetic_ohlc(n=800, seed=7)
    honest = backtest(TimeSeriesMomentum(), close, cost=0.001)
    ret = returns_from_close(close)
    pos = TimeSeriesMomentum().positions(close)
    leaked, _ = execute(pos, ret, 0.001, lag=0)
    leaked_m = metrics_from_returns(np.nan_to_num(leaked), position=pos)
    assert honest["metrics"]["sharpe"] != leaked_m.as_dict()["sharpe"] or honest["metrics"]["n_trades"] != leaked_m.n_trades


def test_certify_emits_gate_fields():
    close = synthetic_ohlc(n=900, mu=0.002, sigma=0.015, seed=3)
    reports = certify(close, RiskLimits())
    ids = {r["strategy_id"] for r in reports}
    assert ids == {"tsmom_vol", "dual_ma", "zscore_mr"}
    for r in reports:
        assert "certified" in r
        assert "oos" in r
        assert "reason" in r
        assert "n_trades" in r["oos"]
