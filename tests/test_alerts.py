"""Out-of-band alerting: detection, severity filter, throttle, channels, CLI.

No network anywhere: every test that could deliver monkeypatches
sovereign.channels.mail.send and/or httpx.post. Worlds run in sim mode
against tmp_path stores, and time only moves when a test moves world.now,
so everything is deterministic and fast.
"""

import json
import sqlite3
from datetime import timedelta

import httpx

from sovereign.alerts import (
    ALERT_STATE_KEY,
    SEVERITY_ORDER,
    Alert,
    AlertManager,
    detect,
)
from sovereign.channels import mail
from sovereign.cli import main
from sovereign.comms.bus import Message
from sovereign.config import AlertConfig, EngineConfig
from sovereign.engine.world import bootstrap

SECRET = "sk-super-secret-token"


def _world(tmp_path):
    return bootstrap(EngineConfig(mode="sim", data_dir=tmp_path))  # type: ignore[arg-type]


def _seed_dead_letter(world, error: str = "handler exploded") -> None:
    receipt = world.comms.send("director", "risk", "ping", {}, now=world.now)
    record = world.store.get_message(receipt.message_ids[0])
    world.comms.dead_letter(Message.from_record(record), error, now=world.now)


def _alert_config(**overrides) -> AlertConfig:
    values = {"enabled": True, "channel": "mail", "to": "ops@example.com"}
    values.update(overrides)
    return AlertConfig(**values)


def _fake_mail(calls):
    def fake_send(world, to=None, subject="", body="", job_id=None, kind="outbound"):
        calls.append({"to": to, "subject": subject, "body": body, "kind": kind})
        return {
            "id": f"mail_fake{len(calls)}",
            "status": "sent_local",
            "address": to,
            "subject": subject,
            "kind": kind,
            "job_id": job_id,
        }

    return fake_send


def _events(world, kind: str):
    return [e for e in world.store.events(500) if e["kind"] == kind]


# ---------------------------------------------------------------------------
# Detection
# ---------------------------------------------------------------------------


def test_detect_clean_world_has_no_alerts_and_is_read_only(tmp_path):
    world = _world(tmp_path)
    before = len(world.store.events(1000))
    assert detect(world) == []
    assert len(world.store.events(1000)) == before  # no events, pure read
    assert world.store.get_kv(ALERT_STATE_KEY) is None


def test_detect_flags_dead_letters(tmp_path):
    world = _world(tmp_path)
    _seed_dead_letter(world)
    alerts = {a.kind: a for a in detect(world)}
    assert set(alerts) == {"dead_letters"}
    alert = alerts["dead_letters"]
    assert alert.severity == "P0"
    assert alert.detail == {"dead": 1}
    assert "1 dead-lettered" in alert.summary


def test_detect_flags_trading_halt_with_reason(tmp_path):
    world = _world(tmp_path)
    world.broker.frozen = True
    world.broker.halt_reason = "daily_halt"
    alerts = {a.kind: a for a in detect(world)}
    assert set(alerts) == {"trading_halt"}
    alert = alerts["trading_halt"]
    assert alert.severity == "P1"
    assert alert.detail["reason"] == "daily_halt"
    assert "daily_halt" in alert.summary


def test_detect_flags_invariant_breach_event(tmp_path):
    world = _world(tmp_path)
    world.store.emit("invariant_breach", {"failed": ["accounting_identity"]}, "auditor")
    alerts = {a.kind: a for a in detect(world)}
    assert set(alerts) == {"invariant_breach"}
    alert = alerts["invariant_breach"]
    assert alert.severity == "P0"
    assert alert.detail["recent_breach_events"] == 1
    assert alert.detail["failing_checks"] == []  # books themselves still verify


def test_detect_flags_tampered_ledger_with_failing_check_names(tmp_path):
    world = _world(tmp_path)
    # Tamper via a separate raw connection (same pattern as the invariants
    # test) so the store's data_version bumps and no balance cache can hide it.
    conn = sqlite3.connect(world.store.db_path)
    try:
        conn.execute(
            "INSERT INTO ledger(ts, debit, credit, amount, memo, ref) VALUES (?,?,?,?,?,?)",
            (world.stamp(), "assets.receivable", "liability.unearned", 100.0, "phantom", None),
        )
        conn.commit()
    finally:
        conn.close()
    alerts = {a.kind: a for a in detect(world)}
    assert set(alerts) == {"invariant_breach"}
    alert = alerts["invariant_breach"]
    assert alert.severity == "P0"
    failing = set(alert.detail["failing_checks"])
    assert {
        "accounting_identity",
        "receivable_matches_open_invoices",
        "unearned_matches_open_invoices",
    } <= failing
    assert "ledger invariants failing" in alert.summary


def test_detect_flags_frozen_agents_on_threshold_or_escalated_kind(tmp_path):
    world = _world(tmp_path)
    world.freeze("hunter", "rep 10 < 20")  # kind: risk
    assert all(a.kind != "agents_frozen" for a in detect(world))  # 1 < threshold

    world.freeze("closer", "rep 5 < 20")
    alerts = {a.kind: a for a in detect(world)}
    assert "agents_frozen" in alerts
    alert = alerts["agents_frozen"]
    assert alert.severity == "P1"
    assert alert.detail["agents"] == {"hunter": "risk", "closer": "risk"}
    assert alert.detail["escalated"] == []

    ethics_world = _world(tmp_path / "ethics")
    ethics_world.freeze("crafter", "ethics: fabricated delivery")
    alerts = {a.kind: a for a in detect(ethics_world)}
    assert "agents_frozen" in alerts  # a single ethics freeze is enough
    assert alerts["agents_frozen"].detail["agents"] == {"crafter": "ethics"}
    assert alerts["agents_frozen"].detail["escalated"] == ["crafter"]


def test_detect_flags_unattributed_payments_in_recent_window(tmp_path):
    world = _world(tmp_path)
    world.store.emit("pay_unattributed", {"chain": "eth", "usd": 25.0}, "courier")
    world.store.emit("pay_unattributed", {"chain": "sol", "usd": 10.0}, "courier")
    alerts = {a.kind: a for a in detect(world)}
    assert set(alerts) == {"payment_unattributed"}
    alert = alerts["payment_unattributed"]
    assert alert.severity == "P1"
    assert alert.detail["count"] == 2

    # Push the events out of the 50-event window; the alert clears.
    for i in range(55):
        world.store.emit("noise", {"i": i}, "director")
    assert detect(world) == []


# ---------------------------------------------------------------------------
# Severity filter and throttle
# ---------------------------------------------------------------------------


def test_min_severity_p0_drops_p1_alerts(tmp_path, monkeypatch):
    world = _world(tmp_path)
    world.broker.frozen = True
    world.broker.halt_reason = "daily_halt"
    calls = []
    monkeypatch.setattr(mail, "send", _fake_mail(calls))

    assert AlertManager(_alert_config(min_severity="P0")).dispatch(world) == []
    assert calls == []

    sent = AlertManager(_alert_config(min_severity="P1")).dispatch(world)
    assert [a.kind for a in sent] == ["trading_halt"]
    assert len(calls) == 1 and calls[0]["subject"] == "[P1] trading_halt"


def test_throttle_suppresses_repeat_then_allows_after_window(tmp_path, monkeypatch):
    world = _world(tmp_path)
    _seed_dead_letter(world)
    calls = []
    monkeypatch.setattr(mail, "send", _fake_mail(calls))
    manager = AlertManager(_alert_config(throttle_minutes=60.0))

    assert [a.kind for a in manager.dispatch(world)] == ["dead_letters"]
    assert manager.dispatch(world) == []  # same now: throttled
    assert len(calls) == 1
    assert "dead_letters" in world.store.get_kv(ALERT_STATE_KEY)

    world.now = world.now + timedelta(minutes=59)
    assert manager.dispatch(world) == []  # still inside the window
    world.now = world.now + timedelta(minutes=2)
    assert [a.kind for a in manager.dispatch(world)] == ["dead_letters"]
    assert len(calls) == 2


# ---------------------------------------------------------------------------
# Channels
# ---------------------------------------------------------------------------


def test_mail_channel_sends_to_recipient_and_never_leaks_secrets(tmp_path, monkeypatch):
    world = _world(tmp_path)
    world.wallet.put_credential("WEBHOOK_HMAC_SECRET", SECRET)
    _seed_dead_letter(world)
    calls = []
    monkeypatch.setattr(mail, "send", _fake_mail(calls))
    config = _alert_config(webhook_url=f"https://hooks.example.com/T/{SECRET}")

    sent = AlertManager(config).dispatch(world)
    assert [a.kind for a in sent] == ["dead_letters"]
    assert len(calls) == 1
    assert calls[0]["to"] == "ops@example.com"
    assert calls[0]["subject"] == "[P0] dead_letters"
    assert calls[0]["kind"] == "alert"
    assert SECRET not in calls[0]["subject"] + calls[0]["body"]

    sent_events = _events(world, "alert_sent")
    assert len(sent_events) == 1
    assert sent_events[0]["payload"]["alert_kind"] == "dead_letters"
    assert sent_events[0]["payload"]["channel"] == "mail"
    assert SECRET not in json.dumps(sent_events)


def test_mail_channel_without_recipient_skips_quietly(tmp_path, monkeypatch):
    world = _world(tmp_path)
    _seed_dead_letter(world)
    calls = []
    monkeypatch.setattr(mail, "send", _fake_mail(calls))

    assert AlertManager(_alert_config(to="")).dispatch(world) == []
    assert calls == []
    assert _events(world, "alert_sent") == [] and _events(world, "alert_error") == []
    assert world.store.get_kv(ALERT_STATE_KEY) is None  # throttle untouched


def test_webhook_channel_posts_json_payload(tmp_path, monkeypatch):
    world = _world(tmp_path)
    _seed_dead_letter(world)
    posts = []

    class _Response:
        def raise_for_status(self):
            return None

    def fake_post(url, json=None, timeout=None, **kwargs):
        posts.append({"url": url, "json": json, "timeout": timeout})
        return _Response()

    monkeypatch.setattr(httpx, "post", fake_post)
    config = _alert_config(channel="webhook", to="", webhook_url="https://hooks.example.com/alerts")

    sent = AlertManager(config).dispatch(world)
    assert [a.kind for a in sent] == ["dead_letters"]
    assert len(posts) == 1
    assert posts[0]["url"] == "https://hooks.example.com/alerts"
    assert posts[0]["timeout"] == 10.0
    payload = posts[0]["json"]
    assert set(payload) == {"severity", "kind", "summary", "detail", "firm", "ts"}
    assert payload["severity"] == "P0" and payload["kind"] == "dead_letters"
    assert payload["detail"] == {"dead": 1}
    assert payload["firm"] == world.config.firm_name
    assert payload["ts"] == world.stamp()


def test_webhook_error_emits_alert_error_without_crashing_or_throttling(tmp_path, monkeypatch):
    world = _world(tmp_path)
    _seed_dead_letter(world)
    url = f"https://hooks.example.com/T/{SECRET}"

    def failing_post(*args, **kwargs):
        raise httpx.ConnectError(f"connection refused for {url}")

    monkeypatch.setattr(httpx, "post", failing_post)
    manager = AlertManager(_alert_config(channel="webhook", to="", webhook_url=url))
    assert manager.dispatch(world) == []  # error is contained, never raised

    errors = _events(world, "alert_error")
    assert len(errors) == 1
    assert errors[0]["payload"]["alert_kind"] == "dead_letters"
    assert SECRET not in json.dumps(errors)  # url redacted from the error text
    assert world.store.get_kv(ALERT_STATE_KEY) is None  # failure never throttles

    posts = []

    class _Response:
        def raise_for_status(self):
            return None

    monkeypatch.setattr(httpx, "post", lambda *a, **k: posts.append(k) or _Response())
    assert [a.kind for a in manager.dispatch(world)] == ["dead_letters"]
    assert len(posts) == 1  # the retry after recovery goes straight out


def test_webhook_channel_without_url_skips_quietly(tmp_path, monkeypatch):
    world = _world(tmp_path)
    _seed_dead_letter(world)
    monkeypatch.setattr(httpx, "post", lambda *a, **k: (_ for _ in ()).throw(AssertionError("no post expected")))
    assert AlertManager(_alert_config(channel="webhook", to="", webhook_url=None)).dispatch(world) == []
    assert _events(world, "alert_sent") == [] and _events(world, "alert_error") == []


def test_disabled_config_detects_but_sends_nothing(tmp_path, monkeypatch):
    world = _world(tmp_path)
    _seed_dead_letter(world)
    calls = []
    monkeypatch.setattr(mail, "send", _fake_mail(calls))
    monkeypatch.setattr(httpx, "post", lambda *a, **k: calls.append(k))

    assert AlertManager(_alert_config(enabled=False)).dispatch(world) == []
    assert calls == []
    assert _events(world, "alert_sent") == [] and _events(world, "alert_error") == []
    assert [a.kind for a in detect(world)] == ["dead_letters"]  # detection still works


def test_dispatch_never_raises_even_when_detection_explodes(tmp_path, monkeypatch):
    world = _world(tmp_path)

    def boom(_world):
        raise RuntimeError("detector exploded")

    monkeypatch.setattr("sovereign.alerts.detect", boom)
    assert AlertManager(_alert_config()).dispatch(world) == []


def test_severity_order_is_total_over_config_levels():
    assert SEVERITY_ORDER == {"P0": 0, "P1": 1, "P2": 2}
    assert Alert("P0", "k", "s", {}).as_dict() == {
        "severity": "P0",
        "kind": "k",
        "summary": "s",
        "detail": {},
    }


# ---------------------------------------------------------------------------
# CLI and daemon wiring
# ---------------------------------------------------------------------------


def test_cli_alerts_prints_detected_alerts_and_sanitized_config(tmp_path, capsys):
    (tmp_path / "config.yaml").write_text(
        "alerts:\n"
        "  enabled: true\n"
        "  channel: mail\n"
        "  to: ops@example.com\n"
        f"  webhook_url: https://hooks.example.com/T/{SECRET}\n"
    )
    world = _world(tmp_path)
    _seed_dead_letter(world)
    world.store.close()

    code = main(["alerts", "--data-dir", str(tmp_path), "--mode", "sim"])
    out = capsys.readouterr().out
    assert code == 0
    payload = json.loads(out)
    assert {a["kind"] for a in payload["alerts"]} == {"dead_letters"}
    assert payload["config"] == {
        "enabled": True,
        "channel": "mail",
        "min_severity": "P0",
        "throttle_minutes": 60.0,
        "to_present": True,
        "webhook_present": True,
    }
    assert SECRET not in out and "hooks.example.com" not in out
    assert "ops@example.com" not in out


def test_cli_alerts_test_delivers_one_synthetic_alert(tmp_path, capsys, monkeypatch):
    (tmp_path / "config.yaml").write_text(
        "alerts:\n  enabled: true\n  channel: mail\n  to: ops@example.com\n"
    )
    calls = []
    monkeypatch.setattr(mail, "send", _fake_mail(calls))

    code = main(["alerts", "--data-dir", str(tmp_path), "--mode", "sim", "--test"])
    assert code == 0
    result = json.loads(capsys.readouterr().out)
    assert result["ok"] is True
    assert result["outcome"] == "sent"
    assert result["channel"] == "mail"
    assert result["alert"]["severity"] == "P0" and result["alert"]["kind"] == "test_alert"
    assert len(calls) == 1
    assert calls[0]["to"] == "ops@example.com"
    assert calls[0]["subject"] == "[P0] test_alert"


def test_daemon_serve_dispatches_alerts_after_each_tick(tmp_path, monkeypatch):
    from sovereign.engine.daemon import serve

    seed = _world(tmp_path)
    _seed_dead_letter(seed)
    seed.store.close()

    calls = []
    monkeypatch.setattr(mail, "send", _fake_mail(calls))
    cfg = EngineConfig(
        mode="sim",
        data_dir=tmp_path,  # type: ignore[arg-type]
        alerts=AlertConfig(enabled=True, channel="mail", to="ops@example.com"),
    )
    serve(cfg, ticks=1, verbose=False)

    alert_mail = [c for c in calls if c["kind"] == "alert"]
    assert alert_mail, "serve loop never dispatched an alert after the tick"
    assert alert_mail[0]["to"] == "ops@example.com"
    assert any(c["subject"] == "[P0] dead_letters" for c in alert_mail)
