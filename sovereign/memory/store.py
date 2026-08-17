from __future__ import annotations

import json
import sqlite3
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterable


def utcnow() -> datetime:
    return datetime.now(timezone.utc)


def iso(dt: datetime | None = None) -> str:
    return (dt or utcnow()).isoformat()


class Store:
    def __init__(self, db_path: Path) -> None:
        self.db_path = db_path
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        self.conn = sqlite3.connect(self.db_path)
        self.conn.row_factory = sqlite3.Row
        self.conn.execute("PRAGMA journal_mode=WAL")
        self._migrate()

    def _migrate(self) -> None:
        self.conn.executescript(
            """
            CREATE TABLE IF NOT EXISTS events (
              id INTEGER PRIMARY KEY AUTOINCREMENT,
              ts TEXT NOT NULL,
              kind TEXT NOT NULL,
              agent TEXT,
              payload TEXT NOT NULL
            );
            CREATE TABLE IF NOT EXISTS ledger (
              id INTEGER PRIMARY KEY AUTOINCREMENT,
              ts TEXT NOT NULL,
              debit TEXT NOT NULL,
              credit TEXT NOT NULL,
              amount REAL NOT NULL,
              memo TEXT NOT NULL,
              ref TEXT
            );
            CREATE TABLE IF NOT EXISTS missions (
              id TEXT PRIMARY KEY,
              play_id TEXT NOT NULL,
              agent TEXT NOT NULL,
              title TEXT NOT NULL,
              status TEXT NOT NULL,
              budget_usd REAL NOT NULL,
              created_ts TEXT NOT NULL,
              payload TEXT NOT NULL
            );
            CREATE TABLE IF NOT EXISTS jobs (
              id TEXT PRIMARY KEY,
              source TEXT NOT NULL,
              title TEXT NOT NULL,
              status TEXT NOT NULL,
              price_usd REAL NOT NULL,
              payload TEXT NOT NULL
            );
            CREATE TABLE IF NOT EXISTS votes (
              id INTEGER PRIMARY KEY AUTOINCREMENT,
              ts TEXT NOT NULL,
              action_id TEXT NOT NULL,
              agent TEXT NOT NULL,
              choice TEXT NOT NULL,
              reason TEXT NOT NULL
            );
            CREATE TABLE IF NOT EXISTS outcomes (
              id INTEGER PRIMARY KEY AUTOINCREMENT,
              ts TEXT NOT NULL,
              play_id TEXT,
              agent TEXT,
              kind TEXT NOT NULL,
              usd REAL NOT NULL,
              success INTEGER NOT NULL,
              note TEXT NOT NULL
            );
            CREATE TABLE IF NOT EXISTS kv (
              k TEXT PRIMARY KEY,
              v TEXT NOT NULL
            );
            """
        )
        self.conn.commit()

    def emit(self, kind: str, payload: dict[str, Any], agent: str | None = None) -> None:
        self.conn.execute(
            "INSERT INTO events(ts, kind, agent, payload) VALUES (?,?,?,?)",
            (iso(), kind, agent, json.dumps(payload)),
        )
        self.conn.commit()

    def events(self, limit: int = 50) -> list[dict[str, Any]]:
        rows = self.conn.execute(
            "SELECT ts, kind, agent, payload FROM events ORDER BY id DESC LIMIT ?",
            (limit,),
        ).fetchall()
        out = []
        for r in rows:
            item = dict(r)
            item["payload"] = json.loads(item["payload"])
            out.append(item)
        return out

    def post_ledger(
        self,
        debit: str,
        credit: str,
        amount: float,
        memo: str,
        ref: str | None = None,
    ) -> None:
        if amount == 0:
            return
        self.conn.execute(
            "INSERT INTO ledger(ts, debit, credit, amount, memo, ref) VALUES (?,?,?,?,?,?)",
            (iso(), debit, credit, float(amount), memo, ref),
        )
        self.conn.commit()

    def ledger_rows(self) -> list[sqlite3.Row]:
        return list(self.conn.execute("SELECT * FROM ledger ORDER BY id ASC"))

    def set_kv(self, k: str, v: Any) -> None:
        self.conn.execute(
            "INSERT INTO kv(k, v) VALUES(?, ?) ON CONFLICT(k) DO UPDATE SET v=excluded.v",
            (k, json.dumps(v)),
        )
        self.conn.commit()

    def get_kv(self, k: str, default: Any = None) -> Any:
        row = self.conn.execute("SELECT v FROM kv WHERE k=?", (k,)).fetchone()
        if not row:
            return default
        return json.loads(row["v"])

    def upsert_job(self, job: dict[str, Any]) -> None:
        self.conn.execute(
            """
            INSERT INTO jobs(id, source, title, status, price_usd, payload)
            VALUES(:id, :source, :title, :status, :price_usd, :payload)
            ON CONFLICT(id) DO UPDATE SET
              status=excluded.status,
              price_usd=excluded.price_usd,
              payload=excluded.payload
            """,
            {
                "id": job["id"],
                "source": job.get("source", "unknown"),
                "title": job.get("title", ""),
                "status": job.get("status", "open"),
                "price_usd": float(job.get("price_usd", 0)),
                "payload": json.dumps(job),
            },
        )
        self.conn.commit()

    def jobs(self, status: str | None = None) -> list[dict[str, Any]]:
        if status:
            rows = self.conn.execute(
                "SELECT payload FROM jobs WHERE status=?", (status,)
            ).fetchall()
        else:
            rows = self.conn.execute("SELECT payload FROM jobs").fetchall()
        return [json.loads(r["payload"]) for r in rows]

    def outcome(
        self,
        kind: str,
        usd: float,
        success: bool,
        note: str,
        agent: str | None = None,
        play_id: str | None = None,
    ) -> None:
        self.conn.execute(
            "INSERT INTO outcomes(ts, play_id, agent, kind, usd, success, note) VALUES (?,?,?,?,?,?,?)",
            (iso(), play_id, agent, kind, float(usd), 1 if success else 0, note),
        )
        self.conn.commit()

    def outcomes(self, limit: int = 100) -> list[dict[str, Any]]:
        rows = self.conn.execute(
            "SELECT * FROM outcomes ORDER BY id DESC LIMIT ?", (limit,)
        ).fetchall()
        return [dict(r) for r in rows]

    def insert_vote(self, action_id: str, agent: str, choice: str, reason: str) -> None:
        self.conn.execute(
            "INSERT INTO votes(ts, action_id, agent, choice, reason) VALUES (?,?,?,?,?)",
            (iso(), action_id, agent, choice, reason),
        )
        self.conn.commit()

    def upsert_mission(self, mission: dict[str, Any]) -> None:
        self.conn.execute(
            """
            INSERT INTO missions(id, play_id, agent, title, status, budget_usd, created_ts, payload)
            VALUES(:id, :play_id, :agent, :title, :status, :budget_usd, :created_ts, :payload)
            ON CONFLICT(id) DO UPDATE SET
              status=excluded.status,
              payload=excluded.payload
            """,
            {
                "id": mission["id"],
                "play_id": mission["play_id"],
                "agent": mission["agent"],
                "title": mission["title"],
                "status": mission["status"],
                "budget_usd": float(mission.get("budget_usd", 0)),
                "created_ts": mission.get("created_ts", iso()),
                "payload": json.dumps(mission),
            },
        )
        self.conn.commit()

    def missions(self, status: str | None = None) -> list[dict[str, Any]]:
        if status:
            rows = self.conn.execute(
                "SELECT payload FROM missions WHERE status=?", (status,)
            ).fetchall()
        else:
            rows = self.conn.execute("SELECT payload FROM missions").fetchall()
        return [json.loads(r["payload"]) for r in rows]

    def close(self) -> None:
        self.conn.close()
