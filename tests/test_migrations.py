"""Versioned schema migrations (PRAGMA user_version) and compaction."""

from __future__ import annotations

import sqlite3

import sovereign.memory.store as store_module
from sovereign.memory.store import Store

EXPECTED_TABLES = {
    "events",
    "ledger",
    "missions",
    "jobs",
    "votes",
    "outcomes",
    "kv",
    "invoices",
    "mail",
    "offers",
    "messages",
    "knowledge",
    "chain_txids",
    "schema_log",
}


def _tables(store: Store) -> set[str]:
    rows = store.conn.execute("SELECT name FROM sqlite_master WHERE type='table'").fetchall()
    return {str(r["name"]) for r in rows}


def test_fresh_store_is_at_current_version_with_full_schema(tmp_path):
    store = Store(tmp_path / "sovereign.db")
    try:
        assert store.schema_version() == store_module.CURRENT_SCHEMA_VERSION
        assert store.schema_version() == len(store_module._MIGRATIONS)
        assert store.schema_version() >= 2
        assert EXPECTED_TABLES <= _tables(store)
        history = store.schema_history()
        assert [row["version"] for row in history] == [1, 2]
        assert history[0]["name"] == "baseline"
        assert history[1]["name"] == "schema_log_and_event_index"
    finally:
        store.close()


def test_reopening_same_db_is_idempotent(tmp_path):
    db = tmp_path / "sovereign.db"
    store = Store(db)
    store.set_kv("alpha", {"n": 1})
    store.post_ledger("cash.operating", "income.labor", 12.5, "job payout")
    version = store.schema_version()
    store.close()

    reopened = Store(db)
    try:
        assert reopened.schema_version() == version
        assert reopened.get_kv("alpha") == {"n": 1}
        assert reopened.ledger_balances()["cash.operating"] == 12.5
        assert EXPECTED_TABLES <= _tables(reopened)
    finally:
        reopened.close()


def test_pre_versioning_db_migrates_to_current_without_data_loss(tmp_path):
    db = tmp_path / "sovereign.db"
    store = Store(db)
    store.set_kv("keep", "me")
    store.upsert_job({"id": "job-1", "source": "sim", "title": "T", "status": "open", "price_usd": 5})
    store.emit("boot", {"ok": True})
    # Rewind the version stamp: tables present but user_version 0 is exactly
    # what a database created by the old unversioned _migrate looks like.
    store.conn.execute("PRAGMA user_version = 0")
    store.conn.commit()
    store.close()

    reopened = Store(db)
    try:
        assert reopened.schema_version() == len(store_module._MIGRATIONS)
        assert reopened.get_kv("keep") == "me"
        job = reopened.get_job("job-1")
        assert job is not None and job["status"] == "open"
        assert reopened.events(5)[0]["kind"] == "boot"
    finally:
        reopened.close()


def test_pending_migrations_apply_in_order_and_bump_version(tmp_path, monkeypatch):
    db = tmp_path / "sovereign.db"
    seeded = Store(db)
    seeded.set_kv("keep", "me")
    baseline_version = seeded.schema_version()
    seeded.close()

    applied: list[str] = []

    def _migration_example_table(conn: sqlite3.Connection) -> None:
        applied.append("table")
        conn.executescript(
            """
            CREATE TABLE IF NOT EXISTS example_notes (
              id INTEGER PRIMARY KEY AUTOINCREMENT,
              note TEXT NOT NULL
            );
            """
        )

    def _migration_example_index(conn: sqlite3.Connection) -> None:
        applied.append("index")
        conn.executescript(
            "CREATE INDEX IF NOT EXISTS idx_example_notes_note ON example_notes(note);"
        )

    monkeypatch.setattr(
        store_module,
        "_MIGRATIONS",
        [*store_module._MIGRATIONS, _migration_example_table, _migration_example_index],
    )

    upgraded = Store(db)
    try:
        assert applied == ["table", "index"]
        assert upgraded.schema_version() == baseline_version + 2
        assert upgraded.schema_version() == len(store_module._MIGRATIONS)
        assert "example_notes" in _tables(upgraded)
        assert upgraded.get_kv("keep") == "me"
        # Idempotent: a second _migrate (the heal path) does not re-apply.
        upgraded._migrate()
        assert applied == ["table", "index"]
        assert upgraded.schema_version() == baseline_version + 2
    finally:
        upgraded.close()


def test_migrate_still_heals_dropped_tables_at_current_version(tmp_path):
    store = Store(tmp_path / "sovereign.db")
    try:
        store.conn.execute("DROP TABLE offers")
        store.conn.commit()
        assert "offers" not in _tables(store)
        store._migrate()  # heal/repair entry point
        assert "offers" in _tables(store)
        assert store.schema_version() == len(store_module._MIGRATIONS)
    finally:
        store.close()


def test_vacuum_runs_and_data_survives(tmp_path):
    store = Store(tmp_path / "sovereign.db")
    try:
        for i in range(25):
            store.emit("tick", {"i": i})
        store.set_kv("keep", "me")
        store.prune_events(5)
        store.vacuum()
        assert store.get_kv("keep") == "me"
        assert len(store.events(100)) == 5
        assert store.schema_version() == len(store_module._MIGRATIONS)
        # Still fully usable after VACUUM + wal_checkpoint.
        store.set_kv("post", "vacuum")
        assert store.get_kv("post") == "vacuum"
    finally:
        store.close()
