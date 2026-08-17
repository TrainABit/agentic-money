from datetime import datetime, timedelta, timezone

from sovereign.capital.invoice import issue, void
from sovereign.capital.payments import watch_and_collect
from sovereign.config import EngineConfig, RiskLimits
from sovereign.engine.world import bootstrap
from sovereign.labor.boards import extract_email
from sovereign.markets.paper import PaperBroker


def test_extract_email_skips_images():
    assert extract_email("write to ada@example.com please") == "ada@example.com"
    assert extract_email("logo https://x.com/a.png no mail") is None


def test_daily_apply_cap_is_per_day_not_lifetime(tmp_path):
    cfg = EngineConfig(mode="live", data_dir=tmp_path, public_job_apis=False, fetch_market_data=False)  # type: ignore[arg-type]
    cfg.daily_apply_cap = 2
    world = bootstrap(cfg)
    world.store.set_kv("apply_by_day", {world.now.date().isoformat(): 2})
    from sovereign.agents.roles import closer

    world.store.upsert_job(
        {
            "id": "job_capcheck01",
            "source": "manual",
            "title": "Python bot",
            "description": "python bot automation contact bob@x.com",
            "status": "open",
            "price_usd": 400,
            "fit": 0.9,
            "contact": "bob@x.com",
        }
    )
    out = closer(world)
    assert out[0]["results"] == []


def test_live_job_without_contact_is_needs_channel(tmp_path):
    cfg = EngineConfig(mode="live", data_dir=tmp_path, public_job_apis=False, fetch_market_data=False)  # type: ignore[arg-type]
    world = bootstrap(cfg)
    world.store.upsert_job(
        {
            "id": "job_noemail01",
            "source": "remoteok",
            "title": "Python automation",
            "description": "python automation no address here",
            "status": "open",
            "price_usd": 400,
            "fit": 0.9,
            "url": "https://example.com/apply",
        }
    )
    from sovereign.agents.roles import closer

    out = closer(world)
    assert out[0]["results"][0]["status"] == "needs_channel"
    assert world.store.get_job("job_noemail01")["status"] == "needs_channel"


def test_paper_halt_rolls_daily_window():
    b = PaperBroker(cash=100)
    b.mark(1.0)
    now = datetime.now(timezone.utc)
    b.roll_windows(now)
    b.cash = 90
    b.mark(1.0)
    assert b.maybe_halt(RiskLimits(daily_halt_pct=0.03), tick=1) == "daily_halt"
    nxt = now + timedelta(days=1)
    b.roll_windows(nxt)
    assert b.day_key == nxt.date().isoformat()
    assert abs(b.day_start_equity - b.equity()) < 1e-9
    b.week_start_equity = b.equity()
    b.halt_tick = 1
    assert b.maybe_unfreeze(RiskLimits(daily_halt_pct=0.03, weekly_halt_pct=0.07), tick=10, cooldown=5)


def test_chain_watch_keeps_unattributed(tmp_path, monkeypatch):
    cfg = EngineConfig(mode="live", data_dir=tmp_path, public_job_apis=False, fetch_market_data=False)  # type: ignore[arg-type]
    world = bootstrap(cfg)
    world.store.upsert_job(
        {
            "id": "job_pay1",
            "source": "manual",
            "title": "Python csv",
            "description": "python csv",
            "status": "delivered",
            "price_usd": 100,
            "fit": 0.9,
        }
    )
    inv = issue(world, world.store.get_job("job_pay1"))
    world.store.set_kv("usdc_attributed", 0.0)
    monkeypatch.setattr("sovereign.capital.payments.usdc_balance", lambda *a, **k: 40.0)
    monkeypatch.setattr("sovereign.capital.payments.sol_usdc_balance", lambda *a, **k: 0.0)
    got = watch_and_collect(world)
    assert got == []
    assert world.store.get_kv("usdc_onchain") == 40.0
    assert inv["status"] == "open"
    monkeypatch.setattr("sovereign.capital.payments.usdc_balance", lambda *a, **k: 100.0)
    got = watch_and_collect(world)
    assert got and got[0]["status"] == "paid"


def test_chain_watch_includes_sol(tmp_path, monkeypatch):
    cfg = EngineConfig(mode="live", data_dir=tmp_path, public_job_apis=False, fetch_market_data=False)  # type: ignore[arg-type]
    world = bootstrap(cfg)
    world.store.upsert_job(
        {
            "id": "job_paysol1",
            "source": "manual",
            "title": "Python csv",
            "description": "python csv",
            "status": "delivered",
            "price_usd": 80,
            "fit": 0.9,
        }
    )
    issue(world, world.store.get_job("job_paysol1"))
    monkeypatch.setattr("sovereign.capital.payments.usdc_balance", lambda *a, **k: 0.0)
    monkeypatch.setattr("sovereign.capital.payments.sol_usdc_balance", lambda *a, **k: 80.0)
    got = watch_and_collect(world)
    assert got and got[0]["status"] == "paid"
    assert world.store.get_kv("usdc_onchain_sol") == 80.0


def test_invoice_void_clears_receivable(tmp_path):
    cfg = EngineConfig(mode="sim", data_dir=tmp_path)  # type: ignore[arg-type]
    world = bootstrap(cfg)
    world.store.upsert_job(
        {
            "id": "job_void01",
            "source": "manual",
            "title": "Python csv",
            "description": "python csv",
            "status": "delivered",
            "price_usd": 200,
            "fit": 0.9,
        }
    )
    inv = issue(world, world.store.get_job("job_void01"))
    assert world.ledger.balance("assets.receivable") == 200
    assert world.ledger.revenue_by_prefix() == 0
    void(world, inv["id"], reason="test")
    assert world.store.get_invoice(inv["id"])["status"] == "void"
    assert abs(world.ledger.balance("assets.receivable")) < 1e-9
    assert world.ledger.revenue_by_prefix() == 0


def test_paper_mtm_excluded_from_trailing_revenue(tmp_path):
    cfg = EngineConfig(mode="sim", data_dir=tmp_path)  # type: ignore[arg-type]
    world = bootstrap(cfg)
    before = world.ledger.trailing_revenue(30, now=world.now)
    world.ledger.post("assets.trading_book", "income.trading_paper", 75, "mtm gain", ts=world.stamp())
    assert world.ledger.trailing_revenue(30, now=world.now) == before
    assert world.ledger.snapshot(now=world.now)["trading_usd"] == 0
    world.ledger.post("assets.usdc", "income.labor", 400, "collect", ts=world.stamp())
    assert world.ledger.trailing_revenue(30, now=world.now) == before + 400


def test_extract_email_skips_images():
    assert extract_email("write to ada@example.com please") == "ada@example.com"
    assert extract_email("logo https://x.com/a.png no mail") is None


def test_daily_apply_cap_is_per_day_not_lifetime(tmp_path):
    cfg = EngineConfig(mode="live", data_dir=tmp_path, public_job_apis=False, fetch_market_data=False)  # type: ignore[arg-type]
    cfg.daily_apply_cap = 2
    world = bootstrap(cfg)
    world.store.set_kv("apply_by_day", {world.now.date().isoformat(): 2})
    from sovereign.agents.roles import closer

    world.store.upsert_job(
        {
            "id": "job_capcheck01",
            "source": "manual",
            "title": "Python bot",
            "description": "python bot automation contact bob@x.com",
            "status": "open",
            "price_usd": 400,
            "fit": 0.9,
            "contact": "bob@x.com",
        }
    )
    out = closer(world)
    assert out[0]["results"] == []


def test_live_job_without_contact_is_needs_channel(tmp_path):
    cfg = EngineConfig(mode="live", data_dir=tmp_path, public_job_apis=False, fetch_market_data=False)  # type: ignore[arg-type]
    world = bootstrap(cfg)
    world.store.upsert_job(
        {
            "id": "job_noemail01",
            "source": "remoteok",
            "title": "Python automation",
            "description": "python automation no address here",
            "status": "open",
            "price_usd": 400,
            "fit": 0.9,
            "url": "https://example.com/apply",
        }
    )
    from sovereign.agents.roles import closer

    out = closer(world)
    assert out[0]["results"][0]["status"] == "needs_channel"
    assert world.store.get_job("job_noemail01")["status"] == "needs_channel"


def test_paper_halt_rolls_daily_window():
    b = PaperBroker(cash=100)
    b.mark(1.0)
    now = datetime.now(timezone.utc)
    b.roll_windows(now)
    b.cash = 90
    b.mark(1.0)
    assert b.maybe_halt(RiskLimits(daily_halt_pct=0.03), tick=1) == "daily_halt"
    nxt = now + timedelta(days=1)
    b.roll_windows(nxt)
    assert b.day_key == nxt.date().isoformat()
    assert abs(b.day_start_equity - b.equity()) < 1e-9
    b.week_start_equity = b.equity()
    b.halt_tick = 1
    assert b.maybe_unfreeze(RiskLimits(daily_halt_pct=0.03, weekly_halt_pct=0.07), tick=10, cooldown=5)


def test_chain_watch_keeps_unattributed(tmp_path, monkeypatch):
    cfg = EngineConfig(mode="live", data_dir=tmp_path, public_job_apis=False, fetch_market_data=False)  # type: ignore[arg-type]
    world = bootstrap(cfg)
    world.store.upsert_job(
        {
            "id": "job_pay1",
            "source": "manual",
            "title": "Python csv",
            "description": "python csv",
            "status": "delivered",
            "price_usd": 100,
            "fit": 0.9,
        }
    )
    inv = issue(world, world.store.get_job("job_pay1"))
    world.store.set_kv("usdc_attributed", 0.0)
    monkeypatch.setattr("sovereign.capital.payments.usdc_balance", lambda *a, **k: 40.0)
    got = watch_and_collect(world)
    assert got == []
    assert world.store.get_kv("usdc_onchain") == 40.0
    assert inv["status"] == "open"
    monkeypatch.setattr("sovereign.capital.payments.usdc_balance", lambda *a, **k: 100.0)
    got = watch_and_collect(world)
    assert got and got[0]["status"] == "paid"
