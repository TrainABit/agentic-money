from concurrent.futures import ThreadPoolExecutor

import httpx
import numpy as np
import pytest
from fastapi.testclient import TestClient

from sovereign.config import EngineConfig, RiskLimits
from sovereign.dashboard.app import HTML, create_app, serve
from sovereign.engine.world import bootstrap
from sovereign.labor.boards import MAX_LIVE_JOBS, MAX_SOURCE_ITEMS, JobBoard
from sovereign.markets.data import (
    MIN_CERTIFICATION_BARS,
    certify,
    synthetic_ohlc,
    validate_closes,
)
from sovereign.markets.stats import execute, metrics_from_returns, rolling_std


def test_dashboard_status_hammer_is_thread_safe(tmp_path, monkeypatch):
    monkeypatch.delenv("SOVEREIGN_DASHBOARD_TOKEN", raising=False)
    bootstrap(EngineConfig(mode="sim", data_dir=tmp_path))
    app = create_app(str(tmp_path), "sim")

    with TestClient(app) as client, ThreadPoolExecutor(max_workers=24) as pool:
        responses = list(pool.map(lambda _: client.get("/api/status"), range(120)))

    assert all(response.status_code == 200 for response in responses)
    assert all("goals" in response.json() for response in responses)


def test_dashboard_bearer_auth_and_non_loopback_guard(tmp_path, monkeypatch):
    bootstrap(EngineConfig(mode="sim", data_dir=tmp_path))
    monkeypatch.setenv("SOVEREIGN_DASHBOARD_TOKEN", "observer-secret")

    with TestClient(create_app(str(tmp_path), "sim")) as client:
        root = client.get("/")
        assert root.status_code == 200
        assert "observer-secret" not in root.text
        assert 'id="observer-token"' in root.text
        assert 'type="password"' in root.text
        missing = client.get("/api/status")
        assert missing.status_code == 401
        assert missing.headers["www-authenticate"] == "Bearer"
        assert client.get(
            "/api/status",
            headers={"Authorization": "Bearer wrong"},
        ).status_code == 401
        authorized = client.get(
            "/api/status",
            headers={"Authorization": "Bearer observer-secret"},
        )
        assert authorized.status_code == 200
        assert authorized.json()["mode"] == "sim"

    monkeypatch.delenv("SOVEREIGN_DASHBOARD_TOKEN")
    with pytest.raises(RuntimeError, match="refusing non-loopback"):
        serve(str(tmp_path), "sim", "0.0.0.0", 7474)


def test_dashboard_html_uses_safe_dom_and_header_token_flow():
    assert "innerHTML" not in HTML
    assert "insertAdjacentHTML" not in HTML
    assert "document.write" not in HTML
    assert "replaceChildren" in HTML
    assert "textContent" in HTML
    assert "'Authorization': 'Bearer ' + token" in HTML
    assert "sessionStorage.setItem(TOKEN_KEY, token)" in HTML
    assert "window.prompt" not in HTML
    assert "?token=" not in HTML


def test_short_history_certification_fails_closed_with_metadata():
    reports = certify(
        synthetic_ohlc(n=MIN_CERTIFICATION_BARS - 1),
        RiskLimits(min_sharpe_oos=-100, max_drawdown_oos=1, min_trades_oos=0),
    )

    assert reports
    for report in reports:
        assert report["certified"] is False
        assert report["oos_method"] == "insufficient_data"
        assert report["oos_windows"] == 0
        assert report["insufficient_data"] == {
            "available_bars": 479,
            "required_bars": 480,
            "train_bars": 400,
            "test_bars": 80,
        }
        assert report["reason"].startswith("insufficient_data:")


def test_close_validation_rejects_bad_values_and_timestamp_order():
    closes = np.linspace(100.0, 200.0, MIN_CERTIFICATION_BARS)
    timestamps = np.arange(MIN_CERTIFICATION_BARS, dtype=float)
    np.testing.assert_array_equal(
        validate_closes(closes, timestamps=timestamps),
        closes,
    )

    with pytest.raises(ValueError, match="too short"):
        validate_closes(closes[:-1], timestamps=timestamps[:-1])
    invalid = closes.copy()
    invalid[20] = np.inf
    with pytest.raises(ValueError, match="non-finite"):
        validate_closes(invalid, timestamps=timestamps)
    unordered = timestamps.copy()
    unordered[200], unordered[201] = unordered[201], unordered[200]
    with pytest.raises(ValueError, match="strictly increasing"):
        validate_closes(closes, timestamps=unordered)


def test_trade_count_uses_economic_state_and_cost_is_round_trip():
    positions = np.array([0.0, 0.4, 0.5, 0.45, 0.0, -0.2, -0.4, 0.3, 0.2, 0.0])
    metrics = metrics_from_returns(np.zeros_like(positions), position=positions)

    assert metrics.n_trades == 5
    assert "positive_bar_rate" in metrics.as_dict()
    assert "hit_rate" not in metrics.as_dict()

    net, _ = execute(
        np.array([0.0, 1.0, 1.0, 0.0]),
        np.zeros(4),
        round_trip_cost=0.01,
        lag=0,
    )
    assert float(np.sum(net)) == pytest.approx(-0.01)


def test_vectorized_rolling_std_matches_reference():
    rng = np.random.default_rng(42)
    values = rng.normal(size=257)
    values[43] = np.nan

    for window in (2, 7, 64, 300):
        expected = np.full_like(values, np.nan)
        for index in range(window - 1, len(values)):
            expected[index] = np.std(values[index - window + 1 : index + 1], ddof=1)
        np.testing.assert_allclose(
            rolling_std(values, window),
            expected,
            rtol=1e-12,
            atol=1e-12,
            equal_nan=True,
        )


def test_job_board_bounds_sources_total_and_reuses_cached_client():
    calls: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append(str(request.url))
        if "arbeitnow" in request.url.host:
            return httpx.Response(
                200,
                json={
                    "data": [
                        {
                            "title": f"Arbeit {index}",
                            "description": "python automation",
                            "url": f"https://example.test/a/{index}",
                        }
                        for index in range(100)
                    ]
                },
            )
        return httpx.Response(
            200,
            json=[
                {
                    "position": f"Remote {index}",
                    "description": "python api",
                    "url": f"https://example.test/r/{index}",
                }
                for index in range(100)
            ],
        )

    with httpx.Client(transport=httpx.MockTransport(handler)) as client:
        board = JobBoard(sim=False, client=client)
        result = board.search_with_metadata(live=True, include_sim=False)
        assert len(result["jobs"]) == MAX_LIVE_JOBS
        assert result["fetch"]["source_counts"] == {
            "arbeitnow": MAX_SOURCE_ITEMS,
            "remoteok": MAX_SOURCE_ITEMS,
        }
        assert result["fetch"]["errors"] == []
        assert len(calls) == 2

        board.search_with_metadata(live=True, include_sim=False)
        assert len(calls) == 2


def test_job_board_returns_structured_errors_and_caches_failures():
    calls = 0

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal calls
        calls += 1
        return httpx.Response(503, text="temporarily unavailable")

    with httpx.Client(transport=httpx.MockTransport(handler)) as client:
        board = JobBoard(sim=False, client=client)
        result = board.search_with_metadata(live=True, include_sim=False)
        assert result["jobs"] == []
        assert result["fetch"]["source_counts"] == {"arbeitnow": 0, "remoteok": 0}
        assert {
            (error["source"], error["error_type"], error["status_code"])
            for error in result["fetch"]["errors"]
        } == {
            ("arbeitnow", "HTTPStatusError", 503),
            ("remoteok", "HTTPStatusError", 503),
        }

        board.search_with_metadata(live=True, include_sim=False)
        assert calls == 2
