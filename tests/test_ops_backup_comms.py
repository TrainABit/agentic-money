"""Bus requeue/prune, cold backups, and the ops CLI + serve readiness gate."""

from __future__ import annotations

import hashlib
import json
import sqlite3
from datetime import datetime, timedelta, timezone
from typing import Any

import pytest

from sovereign.backup import create_backup, verify_backup
from sovereign.cli import main
from sovereign.comms import Bus, Message
from sovereign.config import EngineConfig
from sovereign.engine import daemon as daemon_module
from sovereign.engine.daemon import FileLock, serve
from sovereign.engine.world import bootstrap
from sovereign.memory.store import Store

ROSTER = frozenset({"a", "b", "c", "d"})
BASE = datetime(2026, 1, 1, 12, 0, 0, tzinfo=timezone.utc)


def make_bus(tmp_path) -> tuple[Store, Bus]:
    store = Store(tmp_path / "comms.db")
    return store, Bus(store, ROSTER)


def events_of(store: Store, kind: str) -> list[dict[str, Any]]:
    return [e for e in store.events(limit=1000) if e["kind"] == kind]


# ---------------------------------------------------------------- requeue


def test_requeue_dead_resets_and_emits(tmp_path):
    store, bus = make_bus(tmp_path)
    secret = "kumquat-siren-88"
    bus.send("a", "b", "risky.job", {"secret": secret}, now=BASE, max_attempts=1)
    msg = bus.inbox("b", now=BASE)[0]
    assert bus.fail(msg, "no route", now=BASE) == "dead"

    revived = bus.requeue(msg.id, now=BASE + timedelta(minutes=1))
    assert isinstance(revived, Message)
    assert revived.id == msg.id
    assert revived.status == "queued"
    assert revived.attempts == 0
    assert revived.error is None

    row = store.get_message(msg.id)
    assert (row["status"], row["attempts"], row["error"]) == ("queued", 0, None)
    assert row["payload"] == {"secret": secret}  # payload untouched by requeue

    events = events_of(store, "comms_requeued")
    assert len(events) == 1
    assert events[0]["payload"] == {"id": msg.id, "kind": "risky.job", "recipient": "b"}
    assert secret not in json.dumps(store.events(limit=1000))
    assert [m.id for m in bus.inbox("b", now=BASE + timedelta(minutes=2))] == [msg.id]


def test_requeue_expired_row_returns_to_queue(tmp_path):
    store, bus = make_bus(tmp_path)
    receipt = bus.request(
        "a", "b", "slow.request", {}, now=BASE, deadline=BASE + timedelta(minutes=5)
    )
    assert bus.expire_due(now=BASE + timedelta(minutes=10)) == 1
    revived = bus.requeue(receipt.message_ids[0], now=BASE + timedelta(minutes=11))
    assert revived.status == "queued"
    assert store.get_message(receipt.message_ids[0])["status"] == "queued"


def test_requeue_rejects_done_queued_and_unknown(tmp_path):
    store, bus = make_bus(tmp_path)
    receipt = bus.send("a", "b", "fine.job", {}, now=BASE)
    queued_id = receipt.message_ids[0]
    with pytest.raises(ValueError, match="requeue"):
        bus.requeue(queued_id, now=BASE)
    bus.ack(queued_id, now=BASE)
    with pytest.raises(ValueError, match="requeue"):
        bus.requeue(queued_id, now=BASE)
    with pytest.raises(KeyError):
        bus.requeue("msg_missing", now=BASE)
    assert store.get_message(queued_id)["status"] == "done"
    assert events_of(store, "comms_requeued") == []


# ------------------------------------------------------------------ prune


def test_prune_deletes_only_old_done_expired_and_emits(tmp_path):
    store, bus = make_bus(tmp_path)
    old = BASE
    done_old = bus.send("a", "b", "done.old", {}, now=old)
    bus.ack(done_old.message_ids[0], now=old)
    bus.request("a", "b", "exp.old", {}, now=old, deadline=old + timedelta(hours=1))
    assert bus.expire_due(now=old + timedelta(hours=2)) == 1
    dead_old = bus.send("a", "b", "dead.old", {}, now=old, max_attempts=1)
    bus.fail(Message.from_record(store.get_message(dead_old.message_ids[0])), "gone", now=old)
    queued_old = bus.send("a", "b", "queued.old", {}, now=old)
    fresh = BASE + timedelta(days=20)
    done_fresh = bus.send("a", "b", "done.fresh", {}, now=fresh)
    bus.ack(done_fresh.message_ids[0], now=fresh)

    now = BASE + timedelta(days=21)  # 14-day horizon → cutoff at BASE + 7d
    assert bus.prune(now=now, older_than_days=14.0) == 2
    assert bus.counts() == {"queued": 1, "dead": 1, "done": 1}
    assert store.get_message(queued_old.message_ids[0])["status"] == "queued"
    assert store.get_message(done_fresh.message_ids[0])["status"] == "done"

    pruned_events = events_of(store, "comms_pruned")
    assert len(pruned_events) == 1
    assert pruned_events[0]["payload"] == {"count": 2, "statuses": ["done", "expired"]}

    # A second sweep removes nothing and emits nothing new.
    assert bus.prune(now=now, older_than_days=14.0) == 0
    assert len(events_of(store, "comms_pruned")) == 1

    # Dead rows go only on explicit request.
    assert bus.prune(now=now, older_than_days=14.0, statuses=("dead",)) == 1
    assert bus.counts() == {"queued": 1, "done": 1}
    newest = events_of(store, "comms_pruned")[0]  # events list is newest first
    assert newest["payload"] == {"count": 1, "statuses": ["dead"]}


def test_prune_statuses_validation_rejects_queued(tmp_path):
    store, bus = make_bus(tmp_path)
    bus.send("a", "b", "keep.me", {}, now=BASE - timedelta(days=365))
    for bad in (("queued",), ("done", "queued"), ("nonsense",), ()):
        with pytest.raises(ValueError):
            bus.prune(now=BASE, statuses=bad)
    with pytest.raises(ValueError, match="older_than_days"):
        bus.prune(now=BASE, older_than_days=-1.0)
    assert bus.counts() == {"queued": 1}  # nothing was deleted by rejected calls
    assert events_of(store, "comms_pruned") == []


# ----------------------------------------------------------------- backup


def test_backup_roundtrip_manifest_and_verify_ok(tmp_path):
    cfg = EngineConfig(mode="sim", data_dir=tmp_path / "data")
    world = bootstrap(cfg)
    world.comms.send("mechanic", "bookkeeper", "note.keep", {"k": 1}, now=world.now)

    out = tmp_path / "backup"
    # The world's own store connection stays open: backup must work mid-run.
    manifest = create_backup(cfg, out)

    assert manifest["include_secrets"] is True
    assert manifest["master_key_excluded"] is True
    assert "master.key" in manifest["warning"]
    assert "secrets.enc" in manifest["warning"]
    created = datetime.fromisoformat(manifest["created_ts"])
    assert created.tzinfo is not None
    assert manifest["engine_version"]

    files = manifest["files"]
    assert "sovereign.db" in files
    assert "secrets.enc" in files
    assert any(rel.startswith("playbooks/") for rel in files)
    assert all("master.key" not in rel for rel in files)
    assert not (out / "master.key").exists()

    for rel, meta in files.items():
        blob = (out / rel).read_bytes()
        assert hashlib.sha256(blob).hexdigest() == meta["sha256"]
        assert len(blob) == meta["bytes"]

    report = verify_backup(out)
    assert report == {
        "ok": True,
        "files_checked": len(files),
        "quick_check": "ok",
        "errors": [],
    }

    # The snapshot really carries the engine's data.
    conn = sqlite3.connect(f"file:{(out / 'sovereign.db').as_posix()}?mode=ro", uri=True)
    try:
        n = conn.execute("SELECT COUNT(*) FROM messages WHERE kind='note.keep'").fetchone()[0]
    finally:
        conn.close()
    assert n == 1


def test_backup_no_secrets_and_nonempty_out_refused(tmp_path):
    cfg = EngineConfig(mode="sim", data_dir=tmp_path / "data")
    bootstrap(cfg)
    out = tmp_path / "backup_no_secrets"
    manifest = create_backup(cfg, out, include_secrets=False)
    assert manifest["include_secrets"] is False
    assert "secrets.enc" not in manifest["files"]
    assert not (out / "secrets.enc").exists()
    assert not (out / "master.key").exists()
    assert verify_backup(out)["ok"] is True
    with pytest.raises(ValueError, match="not empty"):
        create_backup(cfg, out)


def test_verify_flags_tampered_missing_and_extra_files(tmp_path):
    cfg = EngineConfig(mode="sim", data_dir=tmp_path / "data")
    bootstrap(cfg)
    out = tmp_path / "backup"
    manifest = create_backup(cfg, out)

    target = out / "secrets.enc"
    raw = bytearray(target.read_bytes())
    raw[0] ^= 0xFF  # flip one byte
    target.write_bytes(bytes(raw))
    report = verify_backup(out)
    assert report["ok"] is False
    assert any("secrets.enc" in error for error in report["errors"])

    raw[0] ^= 0xFF  # restore the byte; verification recovers
    target.write_bytes(bytes(raw))
    assert verify_backup(out)["ok"] is True

    stray = out / "stray.bin"
    stray.write_bytes(b"x")
    report = verify_backup(out)
    assert report["ok"] is False
    assert "unexpected file: stray.bin" in report["errors"]
    stray.unlink()

    victim = next(rel for rel in manifest["files"] if rel.startswith("playbooks/"))
    (out / victim).unlink()
    report = verify_backup(out)
    assert report["ok"] is False
    assert f"missing: {victim}" in report["errors"]


# -------------------------------------------------------------------- CLI


def test_cli_comms_list_hides_payloads_and_orders_newest_first(tmp_path, capsys):
    data = tmp_path / "data"
    world = bootstrap(EngineConfig(mode="sim", data_dir=data))
    canary = "payload-canary-zx91"
    first = world.comms.send(
        "mechanic", "bookkeeper", "alpha.note", {"secret": canary}, now=world.now
    )
    world.comms.send(
        "mechanic",
        "bookkeeper",
        "beta.note",
        {"secret": canary},
        now=world.now + timedelta(seconds=5),
    )
    world.comms.ack(first.message_ids[0], now=world.now + timedelta(seconds=6))

    assert main(["comms", "--data-dir", str(data), "--mode", "sim"]) == 0
    out = capsys.readouterr().out
    assert canary not in out
    rows = json.loads(out)
    assert [row["kind"] for row in rows] == ["beta.note", "alpha.note"]
    for row in rows:
        assert set(row) == {
            "id", "ts", "kind", "sender", "recipient", "status", "attempts", "error",
        }

    assert main(["comms", "--data-dir", str(data), "--mode", "sim", "--status", "queued"]) == 0
    rows = json.loads(capsys.readouterr().out)
    assert [row["kind"] for row in rows] == ["beta.note"]

    assert main(["comms", "--data-dir", str(data), "--mode", "sim", "--limit", "1"]) == 0
    rows = json.loads(capsys.readouterr().out)
    assert [row["kind"] for row in rows] == ["beta.note"]

    assert main(["comms", "--data-dir", str(data), "--mode", "sim", "--status", "bogus"]) == 1
    captured = capsys.readouterr()
    assert "bogus" in captured.err
    assert "Traceback" not in captured.err


def test_cli_comms_requeue_and_purge_flows(tmp_path, capsys):
    data = tmp_path / "data"
    world = bootstrap(EngineConfig(mode="sim", data_dir=data))
    now = world.now
    canary = "requeue-canary-77"
    world.comms.send(
        "mechanic", "bookkeeper", "risky.ping", {"secret": canary}, now=now, max_attempts=1
    )
    msg = world.comms.inbox("bookkeeper", now=now)[0]
    assert world.comms.fail(msg, "no route", now=now) == "dead"
    stale = world.comms.send(
        "mechanic", "bookkeeper", "stale.note", {}, now=now - timedelta(days=40)
    )
    world.comms.ack(stale.message_ids[0], now=now)

    assert main(["comms", "--data-dir", str(data), "--mode", "sim", "--requeue", msg.id]) == 0
    out = capsys.readouterr().out
    row = json.loads(out)
    assert row["id"] == msg.id
    assert row["status"] == "queued"
    assert row["attempts"] == 0
    assert row["error"] is None
    assert "payload" not in row
    assert canary not in out

    assert main(["comms", "--data-dir", str(data), "--mode", "sim", "--purge-days", "30"]) == 0
    assert json.loads(capsys.readouterr().out) == {"pruned": 1}

    assert main(["comms", "--data-dir", str(data), "--mode", "sim"]) == 0
    rows = json.loads(capsys.readouterr().out)
    assert [entry["kind"] for entry in rows] == ["risky.ping"]  # requeued row survived

    assert main(
        ["comms", "--data-dir", str(data), "--mode", "sim", "--requeue", "msg_missing"]
    ) == 1
    captured = capsys.readouterr()
    assert "msg_missing" in captured.err
    assert "Traceback" not in captured.err


def test_cli_backup_out_then_verify_and_flag_conflicts(tmp_path, capsys):
    data = tmp_path / "data"
    assert main(["init", "--data-dir", str(data), "--mode", "sim"]) == 0
    capsys.readouterr()

    out = tmp_path / "bk"
    assert main(["backup", "--data-dir", str(data), "--out", str(out)]) == 0
    manifest = json.loads(capsys.readouterr().out)
    assert "sovereign.db" in manifest["files"]
    assert manifest["master_key_excluded"] is True
    assert not (out / "master.key").exists()

    assert main(["backup", "--data-dir", str(data), "--verify", str(out)]) == 0
    report = json.loads(capsys.readouterr().out)
    assert report["ok"] is True
    assert report["quick_check"] == "ok"

    lean = tmp_path / "bk_lean"
    assert main(["backup", "--data-dir", str(data), "--out", str(lean), "--no-secrets"]) == 0
    manifest = json.loads(capsys.readouterr().out)
    assert "secrets.enc" not in manifest["files"]

    conflicted = main(
        ["backup", "--data-dir", str(data), "--out", str(tmp_path / "bk2"), "--verify", str(out)]
    )
    assert conflicted == 1
    captured = capsys.readouterr()
    assert "mutually exclusive" in captured.err
    assert not (tmp_path / "bk2").exists()

    assert main(["backup", "--data-dir", str(data)]) == 1
    assert "--out" in capsys.readouterr().err


# ------------------------------------------------------------- serve gate


def _live_config(tmp_path) -> EngineConfig:
    """Live config that is deterministic and offline: no claude, no market fetch."""
    return EngineConfig(
        mode="live",
        data_dir=tmp_path,
        fetch_market_data=False,
        models={"claude_bin": "claude-bin-that-does-not-exist"},
    )


def test_serve_live_gate_raises_and_releases_lock(tmp_path):
    cfg = _live_config(tmp_path)
    with pytest.raises(RuntimeError) as excinfo:
        serve(cfg, ticks=1, verbose=False)
    assert "model_provider" in str(excinfo.value)
    lock = FileLock(cfg.paths().lock)
    lock.acquire()  # would raise RuntimeError if serve had leaked its lock
    lock.release()


def test_serve_sim_single_tick_still_runs(tmp_path):
    serve(EngineConfig(mode="sim", data_dir=tmp_path), ticks=1, verbose=False)
    assert (tmp_path / "artifacts" / "health.json").exists()
    lock = FileLock(tmp_path / "engine.lock")
    lock.acquire()
    lock.release()


def test_serve_live_force_overrides_gate(tmp_path, monkeypatch):
    calls = {"n": 0}

    def fake_step(world):
        calls["n"] += 1
        return {
            "tick": calls["n"],
            "actions": 0,
            "equity": 0.0,
            "revenue": 0.0,
            "trailing": 0.0,
            "frozen": [],
            "pipeline": {},
        }

    monkeypatch.setattr(daemon_module, "step", fake_step)
    monkeypatch.setattr(daemon_module.time, "sleep", lambda *_args: None)
    cfg = _live_config(tmp_path)
    serve(cfg, ticks=1, verbose=False, force=True)  # gate warned, did not raise
    assert calls["n"] == 1
    lock = FileLock(cfg.paths().lock)
    lock.acquire()
    lock.release()
