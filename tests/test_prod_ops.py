"""Productionization: maintain, migrate, restore-drill, bootstrap keyring."""

from __future__ import annotations

import json
import sqlite3
import sys
import types

from pathlib import Path

from sovereign.backup import restore_drill
from sovereign.capital.wallet import KeyringMasterKeyStore
from sovereign.cli import main
from sovereign.config import EngineConfig
from sovereign.engine.world import bootstrap
from sovereign.memory.store import CURRENT_SCHEMA_VERSION, Store
from sovereign.ops import maintain
from tests.test_keyring_backend import FakeKeyring


def test_maintain_prunes_comms_and_keeps_usable_store(tmp_path):
    world = bootstrap(EngineConfig(mode="sim", data_dir=tmp_path))  # type: ignore[arg-type]
    old = world.now.replace(year=world.now.year - 1)
    world.store.insert_message(
        {
            "id": "msg_old_done",
            "ts": old.isoformat(),
            "thread_id": "t",
            "correlation_id": "c",
            "sender": "mechanic",
            "recipient": "bookkeeper",
            "kind": "ping",
            "payload": {},
            "status": "done",
        }
    )
    report = maintain(world, vacuum=True, comms_days=30)
    assert report["ok"] is True
    assert report["vacuum"] is True
    assert report["schema_version"] == CURRENT_SCHEMA_VERSION
    assert report["comms_pruned"] == 1
    assert world.store.get_message("msg_old_done") is None
    world.store.set_kv("after", True)
    assert world.store.get_kv("after") is True


def test_cli_migrate_and_maintain(tmp_path, capsys):
    assert main(["init", "--data-dir", str(tmp_path), "--mode", "sim"]) == 0
    capsys.readouterr()
    assert main(["migrate", "--data-dir", str(tmp_path), "--mode", "sim"]) == 0
    payload = json.loads(capsys.readouterr().out)
    assert payload["ok"] is True
    assert payload["schema_version"] == CURRENT_SCHEMA_VERSION
    assert payload["current"] == CURRENT_SCHEMA_VERSION
    assert [row["version"] for row in payload["history"]] == [1, 2]

    assert main(["maintain", "--data-dir", str(tmp_path), "--mode", "sim", "--no-vacuum"]) == 0
    maintained = json.loads(capsys.readouterr().out)
    assert maintained["ok"] is True
    assert maintained["vacuum"] is False


def test_restore_drill_verifies_and_does_not_touch_live(tmp_path):
    data = tmp_path / "data"
    cfg = EngineConfig(mode="sim", data_dir=data)  # type: ignore[arg-type]
    world = bootstrap(cfg)
    world.store.set_kv("marker", {"n": 7})
    live_db = data / "sovereign.db"
    before = live_db.read_bytes()

    report = restore_drill(cfg, tmp_path / "drill")
    assert report["ok"] is True
    assert report["verify"]["ok"] is True
    assert report["probe"]["schema_version"] == CURRENT_SCHEMA_VERSION
    assert "jobs" in report["probe"]["tables"]
    assert "schema_log" in report["probe"]["tables"]
    assert report["manifest"]["master_key_excluded"] is True
    assert live_db.read_bytes() == before
    assert world.store.get_kv("marker") == {"n": 7}


def test_cli_backup_restore_drill(tmp_path, capsys):
    data = tmp_path / "data"
    assert main(["init", "--data-dir", str(data), "--mode", "sim"]) == 0
    capsys.readouterr()
    drill = tmp_path / "drill"
    assert main(["backup", "--data-dir", str(data), "--restore-drill", str(drill)]) == 0
    payload = json.loads(capsys.readouterr().out)
    assert payload["ok"] is True
    assert (drill / "backup" / "sovereign.db").is_file()
    assert not (drill / "backup" / "master.key").exists()


def test_cli_rotate_key_requires_confirm(tmp_path, capsys):
    assert main(["init", "--data-dir", str(tmp_path), "--mode", "sim"]) == 0
    capsys.readouterr()
    assert main(["rotate-key", "--data-dir", str(tmp_path)]) == 1
    assert "--confirm" in capsys.readouterr().err

    assert main(["rotate-key", "--data-dir", str(tmp_path), "--confirm"]) == 0
    report = json.loads(capsys.readouterr().out)
    assert report["ok"] is True
    assert report["backend"] == "file"
    assert main(["wallet", "--data-dir", str(tmp_path)]) == 0


def test_bootstrap_uses_keyring_backend_from_config(tmp_path, monkeypatch):
    fake = FakeKeyring()
    module = types.ModuleType("keyring")
    module.get_password = fake.get_password  # type: ignore[attr-defined]
    module.set_password = fake.set_password  # type: ignore[attr-defined]
    monkeypatch.setitem(sys.modules, "keyring", module)

    cfg = EngineConfig(
        mode="sim",
        data_dir=tmp_path,  # type: ignore[arg-type]
        wallet={"master_key_backend": "keyring"},
    )
    world = bootstrap(cfg)
    assert isinstance(world.wallet.key_store, KeyringMasterKeyStore)
    assert world.wallet.public()["eth_address"].startswith("0x")
    assert not (tmp_path / "master.key").exists()
    assert fake.storage


def test_v1_database_migrates_to_current(tmp_path):
    db = tmp_path / "sovereign.db"
    store = Store(db)
    store.set_kv("keep", "me")
    store.conn.execute("DROP TABLE IF EXISTS schema_log")
    store.conn.execute("PRAGMA user_version = 1")
    store.conn.commit()
    store.close()

    reopened = Store(db)
    try:
        assert reopened.schema_version() == CURRENT_SCHEMA_VERSION
        assert reopened.get_kv("keep") == "me"
        names = {
            str(row["name"])
            for row in reopened.conn.execute(
                "SELECT name FROM sqlite_master WHERE type='table'"
            )
        }
        assert "schema_log" in names
        history = reopened.schema_history()
        assert [row["version"] for row in history] == [1, 2]
    finally:
        reopened.close()


def test_deploy_packaging_artifacts_exist():
    root = Path(__file__).resolve().parents[1]
    dockerfile = (root / "Dockerfile").read_text()
    assert "HEALTHCHECK" in dockerfile
    assert "sovereign healthcheck" in dockerfile
    unit = (root / "deploy" / "sovereign.service").read_text()
    assert "ExecStart=" in unit
    compose = (root / "deploy" / "docker-compose.yml").read_text()
    assert "stale-seconds" in compose


def test_schema_log_survives_direct_sqlite_open(tmp_path):
    db = tmp_path / "sovereign.db"
    store = Store(db)
    store.close()
    conn = sqlite3.connect(db)
    try:
        version = int(conn.execute("PRAGMA user_version").fetchone()[0])
        assert version == CURRENT_SCHEMA_VERSION
        rows = conn.execute("SELECT version, name FROM schema_log ORDER BY version").fetchall()
        assert [tuple(r) for r in rows] == [
            (1, "baseline"),
            (2, "schema_log_and_event_index"),
        ]
    finally:
        conn.close()
