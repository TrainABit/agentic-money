from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta, timezone

import numpy as np
import pytest

from sovereign.agents.roles import (
    auditor,
    closer,
    crafter,
    director,
    improver,
    mechanic,
    publisher,
    risk,
    scout,
    trader,
    treasurer,
)
from sovereign.capital.invoice import issue
from sovereign.config import EngineConfig
from sovereign.engine import heartbeat
from sovereign.engine.world import bootstrap, ensure_certified, load_prices
from sovereign.tools.base import ToolResult


@dataclass
class MutableClock:
    value: datetime

    def now(self) -> datetime:
        return self.value

    def advance(self, **kwargs: float) -> None:
        self.value += timedelta(**kwargs)


def live_world(tmp_path, clock: MutableClock):
    config = EngineConfig(
        mode="live",
        data_dir=tmp_path,
        public_job_apis=False,
        fetch_market_data=False,
        models={"provider": "claude_code", "claude_bin": "__missing_claude__"},
    )
    return bootstrap(config, heal=False, clock=clock)


def sync(world, clock: MutableClock) -> None:
    world.now = clock.now()


def enable_model(world, monkeypatch) -> None:
    monkeypatch.setattr(world.router.claude, "available", lambda: True)
    monkeypatch.setattr(
        world.router.claude,
        "complete",
        lambda prompt, tier, system: "model output",
    )


def enable_outbound(world, monkeypatch) -> None:
    real_use_tool = world.use_tool

    def use_tool(caller, name, /, **kwargs):
        if name == "mail.send":
            return ToolResult(True, data={"status": "sent"})
        return real_use_tool(caller, name, **kwargs)

    monkeypatch.setattr(world, "use_tool", use_tool)


def test_live_expiry_and_invoice_aging_use_elapsed_days(tmp_path, monkeypatch):
    started = datetime(2026, 1, 1, tzinfo=timezone.utc)
    clock = MutableClock(started)
    world = live_world(tmp_path, clock)
    world.store.upsert_job(
        {
            "id": "job_old_proposal",
            "source": "manual",
            "title": "Proposal",
            "status": "applied",
            "price_usd": 100,
            "applied_tick": 1,
            "applied_ts": started.isoformat(),
        }
    )
    world.store.upsert_job(
        {
            "id": "job_old_invoice",
            "source": "manual",
            "title": "Invoice",
            "status": "delivered",
            "price_usd": 200,
        }
    )
    invoice = issue(world, world.store.get_job("job_old_invoice"))
    monkeypatch.setattr("sovereign.agents.roles.watch_and_collect", lambda _world: [])

    world.tick = 10_000
    closer(world)
    treasurer(world)
    assert world.store.get_job("job_old_proposal")["status"] == "applied"
    assert world.store.get_invoice(invoice["id"])["status"] == "open"

    clock.advance(days=14)
    sync(world, clock)
    closer(world)
    treasurer(world)
    assert world.store.get_job("job_old_proposal")["status"] == "expired"
    assert world.store.get_invoice(invoice["id"])["status"] == "open"

    clock.advance(days=76)
    sync(world, clock)
    treasurer(world)
    assert world.store.get_invoice(invoice["id"])["status"] == "void"


def test_live_model_and_heal_cadences_are_bounded(tmp_path, monkeypatch):
    clock = MutableClock(datetime(2026, 2, 1, tzinfo=timezone.utc))
    world = live_world(tmp_path, clock)
    delivery = world.config.paths().deliveries / "result.md"
    delivery.write_text("# Result\n" + "working output\n" * 20)
    calls: list[str] = []

    def complete(prompt, tier="fast", system=""):
        calls.append(prompt)
        return "model output"

    monkeypatch.setattr(world.router, "complete", complete)

    director(world)
    director(world)
    publisher(world)
    publisher(world)
    scout(world)
    scout(world)
    auditor(world)
    auditor(world)
    first_improve = improver(world)
    assert first_improve
    assert improver(world) == []
    first_heal = mechanic(world)
    second_heal = mechanic(world)
    assert first_heal[0]["health"]["full"] is True
    assert second_heal[0]["health"]["full"] is False
    assert len(calls) == 5

    clock.advance(hours=1)
    sync(world, clock)
    auditor(world)
    director(world)
    publisher(world)
    scout(world)
    assert improver(world) == []
    assert mechanic(world)[0]["health"]["full"] is True
    assert len(calls) == 6

    clock.advance(hours=23)
    sync(world, clock)
    publisher(world)
    scout(world)
    director(world)
    assert len(calls) == 8

    clock.advance(days=6)
    sync(world, clock)
    director(world)
    assert improver(world)
    assert len(calls) == 9


def test_play_kill_window_uses_live_elapsed_days(tmp_path, monkeypatch):
    clock = MutableClock(datetime(2026, 3, 1, tzinfo=timezone.utc))
    world = live_world(tmp_path, clock)
    monkeypatch.setattr(world.router, "complete", lambda *args, **kwargs: "trial")
    world.tick = 50_000

    improver(world)
    assert (world.store.get_kv("attention_override") or {}).get("labor_studio") != 0.0

    clock.advance(days=14)
    sync(world, clock)
    improver(world)
    assert world.store.get_kv("attention_override")["labor_studio"] == 0.0


def test_non_auto_freezes_and_broker_wall_cooldown(tmp_path):
    clock = MutableClock(datetime(2026, 4, 1, tzinfo=timezone.utc))
    world = live_world(tmp_path, clock)
    world.freeze("operator", "secret leakage", kind="ethics")
    world.freeze("closer", "human hold", kind="manual")
    world.freeze("auditor", "circuit breaker", kind="circuit_breaker")
    for agent in ("operator", "closer", "auditor"):
        world.reputation.scores[agent] = 100

    clock.advance(days=2)
    sync(world, clock)
    mechanic(world)
    assert {"operator", "closer", "auditor"} <= world.frozen
    world.persist_kv()
    world = bootstrap(world.config, heal=False, clock=clock)
    assert world.freeze_info["operator"]["kind"] == "ethics"
    assert world.freeze_info["operator"]["reason"] == "secret leakage"
    assert {"operator", "closer", "auditor"} <= world.frozen

    world.broker.cash = 100
    world.broker.mark(1.0)
    world.broker.roll_windows(world.now)
    world.broker.cash = 90
    world.broker.mark(1.0)
    risk(world)
    assert world.broker.frozen
    assert "trader" in world.frozen
    assert world.freeze_info["trader"]["kind"] == "circuit_breaker"
    assert world.freeze_info["trader"]["reason"] == "daily_halt"
    world.broker.week_start_equity = world.broker.equity()

    clock.advance(hours=23)
    sync(world, clock)
    risk(world)
    assert world.broker.frozen
    assert "trader" in world.frozen

    clock.advance(hours=1)
    sync(world, clock)
    risk(world)
    assert not world.broker.frozen
    assert "trader" not in world.frozen


def test_queued_budget_retries_after_budget_recovers(tmp_path, monkeypatch):
    clock = MutableClock(datetime(2026, 5, 1, tzinfo=timezone.utc))
    world = live_world(tmp_path, clock)
    enable_model(world, monkeypatch)
    enable_outbound(world, monkeypatch)
    world.config.models.daily_token_budget = 10
    world.store.upsert_job(
        {
            "id": "job_budget_retry",
            "source": "manual",
            "title": "Python automation",
            "description": "Build an automation",
            "status": "open",
            "price_usd": 500,
            "fit": 0.9,
            "contact": "buyer@example.com",
        }
    )

    closer(world)
    assert world.store.get_job("job_budget_retry")["status"] == "queued_budget"

    world.config.models.daily_token_budget = 100_000
    world.router.usage_day = ""
    world.router.degraded = True
    closer(world)
    assert world.store.get_job("job_budget_retry")["status"] == "applied"


def test_needs_channel_does_not_consume_successful_send_cap(tmp_path, monkeypatch):
    clock = MutableClock(datetime(2026, 6, 1, tzinfo=timezone.utc))
    world = live_world(tmp_path, clock)
    enable_model(world, monkeypatch)
    enable_outbound(world, monkeypatch)
    world.config.daily_apply_cap = 1
    world.store.upsert_job(
        {
            "id": "job_no_channel",
            "source": "manual",
            "title": "No channel",
            "description": "No address",
            "status": "open",
            "price_usd": 100,
            "fit": 1.0,
        }
    )
    world.store.upsert_job(
        {
            "id": "job_with_channel",
            "source": "manual",
            "title": "Has channel",
            "description": "Python automation",
            "status": "open",
            "price_usd": 100,
            "fit": 0.9,
            "contact": "buyer@example.com",
        }
    )

    closer(world)
    assert world.store.get_job("job_no_channel")["status"] == "needs_channel"
    assert world.store.get_job("job_with_channel")["status"] == "applied"
    assert world.store.get_kv("apply_by_day")[world.now.date().isoformat()] == 1


def test_failed_state_changing_tool_is_not_bypassed(tmp_path, monkeypatch):
    clock = MutableClock(datetime(2026, 7, 1, tzinfo=timezone.utc))
    world = live_world(tmp_path, clock)
    enable_model(world, monkeypatch)
    world.store.upsert_job(
        {
            "id": "job_send_failure",
            "source": "manual",
            "title": "Mail failure",
            "description": "Python automation",
            "status": "open",
            "price_usd": 100,
            "fit": 0.9,
            "contact": "buyer@example.com",
        }
    )
    real_use_tool = world.use_tool

    def fail_mail(caller, name, /, **kwargs):
        if name == "mail.send":
            return ToolResult(False, error="injected send failure")
        return real_use_tool(caller, name, **kwargs)

    monkeypatch.setattr(world, "use_tool", fail_mail)
    result = closer(world)[0]["results"]
    assert result[0]["operation"] == "mail.send"
    assert world.store.get_job("job_send_failure")["status"] == "open"
    assert world.store.mail(direction="out") == []


def test_crash_tick_marker_advances_after_restart(tmp_path, monkeypatch):
    config = EngineConfig(mode="sim", data_dir=tmp_path)
    world = bootstrap(config)
    initial_now = world.now
    real_load_prices = heartbeat.load_prices

    def crash(_world):
        raise RuntimeError("injected tick crash")

    monkeypatch.setattr(heartbeat, "load_prices", crash)
    with pytest.raises(RuntimeError, match="injected tick crash"):
        heartbeat.step(world)
    marker = world.store.get_kv("tick_start")
    assert marker["tick"] == 1
    assert marker["status"] == "started"

    restarted = bootstrap(config, heal=False)
    assert restarted.tick == 1
    assert restarted.now == initial_now + timedelta(hours=config.tick_hours)
    monkeypatch.setattr(heartbeat, "load_prices", real_load_prices)
    result = heartbeat.step(restarted)
    assert result["tick"] == 2
    assert restarted.now == initial_now + timedelta(hours=2 * config.tick_hours)
    assert restarted.store.get_kv("tick_start")["status"] == "completed"


def test_trader_selects_best_sharpe_and_honors_risk_caps(tmp_path):
    clock = MutableClock(datetime(2026, 8, 1, tzinfo=timezone.utc))
    world = live_world(tmp_path, clock)
    world.config.risk.trading_risk_per_signal = 0.02
    world.config.risk.hot_wallet_cap_usd = 75
    world.broker.cash = 10_000
    world.market_close = np.linspace(100.0, 500.0, 300).tolist()
    world.certified = [
        {"strategy_id": "dual_ma", "certified": True, "oos": {"sharpe": 0.6}},
        {"strategy_id": "tsmom_vol", "certified": True, "oos": {"sharpe": 1.8}},
    ]

    result = trader(world)[0]
    assert result["strategy"] == "tsmom_vol"
    assert result["position_cap_usd"] == 75
    assert abs(world.broker.position * world.broker.last_price) <= 75.000001

    world.broker.cash = 10_000
    world.broker.position = 0
    world.config.risk.hot_wallet_cap_usd = 1_000
    world.config.risk.trading_risk_per_signal = 0.005
    risk_limited = trader(world)[0]
    assert risk_limited["position_cap_usd"] == 50
    assert abs(world.broker.position * world.broker.last_price) <= 50.000001

    world.broker.position = 0
    world.certified = [
        {"strategy_id": "dual_ma", "certified": True, "oos": {"sharpe": 2.0}},
    ]
    world.market_close = np.linspace(100.0, 200.0, 200).tolist()
    warmup = trader(world)[0]
    assert warmup["skipped"] == "warmup"
    assert warmup["required"] == 201


def test_remote_public_contacts_require_explicit_verification(tmp_path, monkeypatch):
    clock = MutableClock(datetime(2026, 9, 1, tzinfo=timezone.utc))
    world = live_world(tmp_path, clock)
    enable_model(world, monkeypatch)
    sent_to = []
    real_use_tool = world.use_tool

    def capture_mail(caller, name, /, **kwargs):
        if name == "mail.send":
            sent_to.append(kwargs["to"])
            return ToolResult(True, data={"status": "sent"})
        return real_use_tool(caller, name, **kwargs)

    monkeypatch.setattr(world, "use_tool", capture_mail)
    jobs = [
        {
            "id": "job_scraped_contact",
            "source": "remoteok",
            "title": "Scraped contact",
            "description": "Email leaked@example.com",
            "status": "open",
            "price_usd": 100,
            "fit": 1.0,
            "remote": True,
            "contact": "leaked@example.com",
        },
        {
            "id": "job_extracted_contact",
            "source": "arbeitnow",
            "title": "Extracted only",
            "description": "Email extracted@example.com",
            "status": "open",
            "price_usd": 100,
            "fit": 0.95,
            "remote": True,
            "contact_verified": True,
        },
        {
            "id": "job_verified_contact",
            "source": "manual-review",
            "title": "Verified contact",
            "description": "Reviewed public listing",
            "status": "open",
            "price_usd": 100,
            "fit": 0.9,
            "remote": True,
            "contact_verified": True,
            "contact": "verified@example.com",
        },
    ]
    for job in jobs:
        world.store.upsert_job(job)

    closer(world)
    assert world.store.get_job("job_scraped_contact")["status"] == "needs_channel"
    assert world.store.get_job("job_extracted_contact")["status"] == "needs_channel"
    assert world.store.get_job("job_verified_contact")["status"] == "applied"
    assert sent_to == ["verified@example.com"]


def test_sim_rejection_uses_pipeline_transition(tmp_path):
    config = EngineConfig(mode="sim", data_dir=tmp_path)
    config.sim.close_rate = 0
    world = bootstrap(config, heal=False)
    world.store.upsert_job(
        {
            "id": "job_simreject1",
            "source": "sim-market",
            "title": "Reject through pipeline",
            "description": "Python automation",
            "status": "open",
            "price_usd": 100,
            "fit": 0.9,
        }
    )

    closer(world)
    rejected = world.store.get_job("job_simreject1")
    assert rejected["status"] == "rejected"
    assert rejected["rejected_via"] == "sim"
    matching = [
        outcome
        for outcome in world.store.outcomes(20)
        if outcome.get("note") == "Reject through pipeline"
    ]
    assert len(matching) == 1


def test_queued_craft_waits_for_daily_budget_recovery(tmp_path, monkeypatch):
    clock = MutableClock(datetime(2026, 10, 1, tzinfo=timezone.utc))
    world = live_world(tmp_path, clock)
    world.router.usage_day = datetime.now(timezone.utc).date().isoformat()
    world.store.upsert_job(
        {
            "id": "job_craft_budget1",
            "source": "manual",
            "title": "Queued craft",
            "status": "accepted",
            "price_usd": 100,
        }
    )
    calls = []
    real_use_tool = world.use_tool

    def craft_result(caller, name, /, **kwargs):
        if name == "craft.produce":
            calls.append(name)
            if len(calls) == 1:
                world.router.degraded = True
                world.router.last_error = "daily model budget exhausted"
                return ToolResult(
                    True,
                    data={"queued": True, "delivery": None, "files": []},
                )
            return ToolResult(
                True,
                data={"delivery": str(tmp_path / "delivery"), "files": ["result.txt"]},
            )
        return real_use_tool(caller, name, **kwargs)

    monkeypatch.setattr(world, "use_tool", craft_result)
    first = crafter(world)[0]
    assert first["status"] == "queued_craft"
    assert world.store.get_job("job_craft_budget1")["status"] == "queued_craft"

    second = crafter(world)[0]
    assert second["skipped"] == "router_degraded"
    assert len(calls) == 1

    world.router.usage_day = ""
    crafter(world)
    assert len(calls) == 2
    assert world.store.get_job("job_craft_budget1")["status"] == "delivered"


def test_queued_craft_provider_retry_is_bounded(tmp_path, monkeypatch):
    clock = MutableClock(datetime(2026, 10, 2, tzinfo=timezone.utc))
    world = live_world(tmp_path, clock)
    world.router.usage_day = datetime.now(timezone.utc).date().isoformat()
    world.store.upsert_job(
        {
            "id": "job_craft_provider1",
            "source": "manual",
            "title": "Provider retry",
            "status": "accepted",
            "price_usd": 100,
        }
    )
    calls = []
    provider = {"ready": False}
    monkeypatch.setattr(world.router.claude, "available", lambda: provider["ready"])
    real_use_tool = world.use_tool

    def craft_result(caller, name, /, **kwargs):
        if name == "craft.produce":
            calls.append(name)
            if len(calls) == 1:
                world.router.degraded = True
                world.router.last_error = "claude invocation failed"
                return ToolResult(
                    True,
                    data={"queued": True, "delivery": None, "files": []},
                )
            return ToolResult(
                True,
                data={"delivery": str(tmp_path / "delivery"), "files": ["result.txt"]},
            )
        return real_use_tool(caller, name, **kwargs)

    monkeypatch.setattr(world, "use_tool", craft_result)
    crafter(world)
    crafter(world)
    provider["ready"] = True
    crafter(world)
    assert len(calls) == 1

    clock.advance(hours=world.config.live_timing.craft_retry_hours)
    sync(world, clock)
    crafter(world)
    assert len(calls) == 2
    assert world.store.get_job("job_craft_provider1")["status"] == "delivered"


def test_tick_completion_preserves_started_timestamp(tmp_path):
    clock = MutableClock(datetime(2026, 10, 3, tzinfo=timezone.utc))
    world = live_world(tmp_path, clock)
    world.start_tick()
    started = world.store.get_kv("tick_start")
    clock.advance(minutes=1)
    world.finish_tick()
    completed = world.store.get_kv("tick_start")

    assert completed["started_ts"] == started["started_ts"]
    assert completed["completed_ts"] == clock.now().isoformat()
    assert completed["status"] == "completed"


def test_dedicated_certification_survives_pre_meta_crash(tmp_path):
    clock = MutableClock(datetime(2026, 11, 1, tzinfo=timezone.utc))
    config = EngineConfig(mode="live", data_dir=tmp_path / "newer")
    world = bootstrap(config, heal=False, clock=clock)
    old = [{"strategy_id": "old", "certified": True}]
    new = [{"strategy_id": "new", "certified": True}]
    world.certified = old
    world.store.set_kv("certified", old)
    world.store.set_kv("certified_ts", world.stamp())
    world.persist_kv()

    clock.advance(hours=1)
    sync(world, clock)
    world.store.set_kv("certified", new)
    world.store.set_kv("certified_ts", world.stamp())
    restarted = bootstrap(config, heal=False, clock=clock)
    assert restarted.certified == new

    no_meta_config = EngineConfig(mode="live", data_dir=tmp_path / "no-meta")
    no_meta = bootstrap(no_meta_config, heal=False, clock=clock)
    no_meta.store.set_kv("certified", new)
    no_meta.store.set_kv("certified_ts", no_meta.stamp())
    no_meta_restarted = bootstrap(no_meta_config, heal=False, clock=clock)
    assert no_meta_restarted.certified == new


def test_live_market_failures_use_short_bounded_retries(tmp_path, monkeypatch):
    clock = MutableClock(datetime(2026, 12, 1, tzinfo=timezone.utc))
    world = live_world(tmp_path, clock)
    world.config.fetch_market_data = True
    world.config.live_timing.price_refresh_hours = 24
    world.config.live_timing.price_failure_retry_minutes = 5
    world.config.live_timing.certification_retry_hours = 24
    world.config.live_timing.certification_failure_retry_minutes = 10
    price_attempts = []

    def fail_prices():
        price_attempts.append(world.now)
        raise RuntimeError("price source unavailable")

    monkeypatch.setattr("sovereign.engine.world.fetch_closes", fail_prices)
    load_prices(world)
    clock.advance(minutes=4)
    sync(world, clock)
    load_prices(world)
    assert len(price_attempts) == 1
    clock.advance(minutes=1)
    sync(world, clock)
    load_prices(world)
    assert len(price_attempts) == 2

    world.market_close = np.linspace(100.0, 200.0, 300).tolist()
    world.last_prices["source"] = "verified-test-source"
    cert_attempts = []

    def fail_certification(*args, **kwargs):
        cert_attempts.append(world.now)
        raise RuntimeError("certification unavailable")

    monkeypatch.setattr("sovereign.engine.world.certify", fail_certification)
    ensure_certified(world)
    clock.advance(minutes=9)
    sync(world, clock)
    ensure_certified(world)
    assert len(cert_attempts) == 1
    clock.advance(minutes=1)
    sync(world, clock)
    ensure_certified(world)
    assert len(cert_attempts) == 2
