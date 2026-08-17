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
        self.conn = sqlite3.connect(self.db_path, check_same_thread=False, timeout=30.0)
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
            CREATE TABLE IF NOT EXISTS invoices (
              id TEXT PRIMARY KEY,
              ts TEXT NOT NULL,
              job_id TEXT NOT NULL,
              amount REAL NOT NULL,
              status TEXT NOT NULL,
              income_account TEXT NOT NULL,
              payload TEXT NOT NULL
            );
            CREATE TABLE IF NOT EXISTS mail (
              id TEXT PRIMARY KEY,
              ts TEXT NOT NULL,
              direction TEXT NOT NULL,
              address TEXT NOT NULL,
              subject TEXT NOT NULL,
              status TEXT NOT NULL,
              payload TEXT NOT NULL
            );
            CREATE TABLE IF NOT EXISTS offers (
              id TEXT PRIMARY KEY,
              ts TEXT NOT NULL,
              title TEXT NOT NULL,
              kind TEXT NOT NULL,
              price_usd REAL NOT NULL,
              status TEXT NOT NULL,
              payload TEXT NOT NULL
            );
            CREATE INDEX IF NOT EXISTS idx_jobs_status ON jobs(status);
            CREATE INDEX IF NOT EXISTS idx_invoices_status ON invoices(status);
            CREATE INDEX IF NOT EXISTS idx_events_kind ON events(kind);
            CREATE INDEX IF NOT EXISTS idx_ledger_ts ON ledger(ts);
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
        ts: str | None = None,
    ) -> None:
        if amount == 0:
            return
        self.conn.execute(
            "INSERT INTO ledger(ts, debit, credit, amount, memo, ref) VALUES (?,?,?,?,?,?)",
            (ts or iso(), debit, credit, float(amount), memo, ref),
        )
        self.conn.commit()

    def ledger_rows(self, since: str | None = None) -> list[sqlite3.Row]:
        if since:
            return list(self.conn.execute("SELECT * FROM ledger WHERE ts >= ? ORDER BY id ASC", (since,)))
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

    def get_job(self, job_id: str) -> dict[str, Any] | None:
        row = self.conn.execute("SELECT payload FROM jobs WHERE id=?", (job_id,)).fetchone()
        return json.loads(row["payload"]) if row else None

    def job_counts(self) -> dict[str, int]:
        rows = self.conn.execute("SELECT status, COUNT(*) AS n FROM jobs GROUP BY status").fetchall()
        return {r["status"]: int(r["n"]) for r in rows}

    def upsert_invoice(self, inv: dict[str, Any]) -> None:
        self.conn.execute(
            """
            INSERT INTO invoices(id, ts, job_id, amount, status, income_account, payload)
            VALUES(:id, :ts, :job_id, :amount, :status, :income_account, :payload)
            ON CONFLICT(id) DO UPDATE SET
              status=excluded.status,
              payload=excluded.payload
            """,
            {
                "id": inv["id"],
                "ts": inv.get("ts", iso()),
                "job_id": inv["job_id"],
                "amount": float(inv["amount"]),
                "status": inv["status"],
                "income_account": inv.get("income_account", "income.labor"),
                "payload": json.dumps(inv),
            },
        )
        self.conn.commit()

    def invoices(self, status: str | None = None) -> list[dict[str, Any]]:
        if status:
            rows = self.conn.execute(
                "SELECT payload FROM invoices WHERE status=?", (status,)
            ).fetchall()
        else:
            rows = self.conn.execute("SELECT payload FROM invoices ORDER BY ts ASC").fetchall()
        return [json.loads(r["payload"]) for r in rows]

    def get_invoice(self, invoice_id: str) -> dict[str, Any] | None:
        row = self.conn.execute("SELECT payload FROM invoices WHERE id=?", (invoice_id,)).fetchone()
        return json.loads(row["payload"]) if row else None

    def invoice_for_job(self, job_id: str) -> dict[str, Any] | None:
        row = self.conn.execute(
            "SELECT payload FROM invoices WHERE job_id=? ORDER BY ts DESC LIMIT 1",
            (job_id,),
        ).fetchone()
        return json.loads(row["payload"]) if row else None

    def upsert_mail(self, msg: dict[str, Any]) -> None:
        self.conn.execute(
            """
            INSERT INTO mail(id, ts, direction, address, subject, status, payload)
            VALUES(:id, :ts, :direction, :address, :subject, :status, :payload)
            ON CONFLICT(id) DO UPDATE SET
              status=excluded.status,
              payload=excluded.payload
            """,
            {
                "id": msg["id"],
                "ts": msg.get("ts", iso()),
                "direction": msg.get("direction", "out"),
                "address": msg.get("address", ""),
                "subject": msg.get("subject", ""),
                "status": msg.get("status", "queued"),
                "payload": json.dumps(msg),
            },
        )
        self.conn.commit()

    def mail(self, direction: str | None = None, status: str | None = None) -> list[dict[str, Any]]:
        q = "SELECT payload FROM mail WHERE 1=1"
        args: list[Any] = []
        if direction:
            q += " AND direction=?"
            args.append(direction)
        if status:
            q += " AND status=?"
            args.append(status)
        q += " ORDER BY ts DESC"
        return [json.loads(r["payload"]) for r in self.conn.execute(q, args).fetchall()]

    def upsert_offer(self, offer: dict[str, Any]) -> None:
        self.conn.execute(
            """
            INSERT INTO offers(id, ts, title, kind, price_usd, status, payload)
            VALUES(:id, :ts, :title, :kind, :price_usd, :status, :payload)
            ON CONFLICT(id) DO UPDATE SET
              status=excluded.status,
              payload=excluded.payload
            """,
            {
                "id": offer["id"],
                "ts": offer.get("ts", iso()),
                "title": offer["title"],
                "kind": offer.get("kind", "fixed"),
                "price_usd": float(offer.get("price_usd", 0)),
                "status": offer.get("status", "listed"),
                "payload": json.dumps(offer),
            },
        )
        self.conn.commit()

    def offers(self, status: str | None = None) -> list[dict[str, Any]]:
        if status:
            rows = self.conn.execute(
                "SELECT payload FROM offers WHERE status=?", (status,)
            ).fetchall()
        else:
            rows = self.conn.execute("SELECT payload FROM offers").fetchall()
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
