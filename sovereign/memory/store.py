from __future__ import annotations

from contextlib import contextmanager
from decimal import Decimal, InvalidOperation, ROUND_HALF_UP
import json
import sqlite3
import threading
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterator, Sequence


def utcnow() -> datetime:
    return datetime.now(timezone.utc)


def iso(dt: datetime | None = None) -> str:
    return (dt or utcnow()).isoformat()


def _decimal(value: Any) -> Decimal:
    try:
        result = Decimal(str(value))
    except (InvalidOperation, TypeError, ValueError) as exc:
        raise ValueError(f"invalid monetary amount: {value!r}") from exc
    if not result.is_finite():
        raise ValueError(f"invalid monetary amount: {value!r}")
    return result


def usd_minor(value: Any) -> int:
    """Return USD cents using deterministic commercial rounding."""
    return int((_decimal(value) * 100).quantize(Decimal("1"), rounding=ROUND_HALF_UP))


def usd_amount(value: Any) -> float:
    """Normalize a persisted USD value to cents."""
    return usd_minor(value) / 100


def usdc_minor(value: Any) -> int:
    """Return USDC's six-decimal integer representation."""
    return int((_decimal(value) * 1_000_000).quantize(Decimal("1"), rounding=ROUND_HALF_UP))


def usdc_amount(value: int) -> float:
    return int(value) / 1_000_000


class Store:
    def __init__(self, db_path: Path, event_retention: int | None = 10_000) -> None:
        self.db_path = db_path
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        self.event_retention = event_retention
        self._lock = threading.RLock()
        self._local = threading.local()
        self._savepoint_seq = 0
        self._ledger_revision = 0
        self.conn = sqlite3.connect(self.db_path, check_same_thread=False, timeout=30.0)
        self.conn.row_factory = sqlite3.Row
        self.conn.execute("PRAGMA busy_timeout=30000")
        self.conn.execute("PRAGMA journal_mode=WAL")
        self._migrate()

    def _transaction_depth(self) -> int:
        return int(getattr(self._local, "transaction_depth", 0))

    @contextmanager
    def transaction(self, *, immediate: bool = True) -> Iterator[None]:
        """Run writes atomically, nesting safely through SQLite savepoints.

        The process-local re-entrant lock protects the shared connection. At the
        outer boundary, BEGIN IMMEDIATE obtains SQLite's cross-connection write
        reservation before callers perform their read/check/write sequence.
        """
        self._lock.acquire()
        depth = self._transaction_depth()
        savepoint: str | None = None
        try:
            if depth == 0:
                self.conn.execute("BEGIN IMMEDIATE" if immediate else "BEGIN")
            else:
                self._savepoint_seq += 1
                savepoint = f"store_sp_{self._savepoint_seq}"
                self.conn.execute(f"SAVEPOINT {savepoint}")
            self._local.transaction_depth = depth + 1
            try:
                yield
            except BaseException:
                if savepoint is None:
                    self.conn.rollback()
                else:
                    self.conn.execute(f"ROLLBACK TO SAVEPOINT {savepoint}")
                    self.conn.execute(f"RELEASE SAVEPOINT {savepoint}")
                # A balance may have been read and cached while this transaction
                # contained uncommitted ledger rows.
                self._ledger_revision += 1
                raise
            else:
                try:
                    if savepoint is None:
                        self.conn.commit()
                    else:
                        self.conn.execute(f"RELEASE SAVEPOINT {savepoint}")
                except BaseException:
                    if savepoint is None:
                        self.conn.rollback()
                    self._ledger_revision += 1
                    raise
        finally:
            self._local.transaction_depth = depth
            self._lock.release()

    def _execute_write(self, sql: str, params: Sequence[Any] | dict[str, Any] = ()) -> None:
        with self._lock:
            try:
                self.conn.execute(sql, params)
                if self._transaction_depth() == 0:
                    self.conn.commit()
            except BaseException:
                if self._transaction_depth() == 0:
                    self.conn.rollback()
                raise

    def _migrate(self) -> None:
        with self._lock:
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
        encoded = json.dumps(payload)
        with self._lock:
            try:
                self.conn.execute(
                    "INSERT INTO events(ts, kind, agent, payload) VALUES (?,?,?,?)",
                    (iso(), kind, agent, encoded),
                )
                self._prune_events_locked(self.event_retention)
                if self._transaction_depth() == 0:
                    self.conn.commit()
            except BaseException:
                if self._transaction_depth() == 0:
                    self.conn.rollback()
                raise

    def _prune_events_locked(self, keep: int | None) -> None:
        if keep is None:
            return
        if keep < 0:
            raise ValueError("event retention must be >= 0 or None")
        if keep == 0:
            self.conn.execute("DELETE FROM events")
            return
        self.conn.execute(
            """
            DELETE FROM events
            WHERE id < (
              SELECT id FROM events ORDER BY id DESC LIMIT 1 OFFSET ?
            )
            """,
            (keep - 1,),
        )

    def prune_events(self, keep: int | None = None) -> None:
        with self._lock:
            try:
                self._prune_events_locked(self.event_retention if keep is None else keep)
                if self._transaction_depth() == 0:
                    self.conn.commit()
            except BaseException:
                if self._transaction_depth() == 0:
                    self.conn.rollback()
                raise

    def events(self, limit: int = 50) -> list[dict[str, Any]]:
        with self._lock:
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
        if _decimal(amount) < 0:
            raise ValueError("ledger amount must be >= 0")
        amount = usd_amount(amount)
        if amount == 0:
            return
        with self._lock:
            try:
                self.conn.execute(
                    "INSERT INTO ledger(ts, debit, credit, amount, memo, ref) VALUES (?,?,?,?,?,?)",
                    (ts or iso(), debit, credit, amount, memo, ref),
                )
                self._ledger_revision += 1
                if self._transaction_depth() == 0:
                    self.conn.commit()
            except BaseException:
                if self._transaction_depth() == 0:
                    self.conn.rollback()
                raise

    def ledger_rows(self, since: str | None = None) -> list[sqlite3.Row]:
        with self._lock:
            if since:
                return list(self.conn.execute("SELECT * FROM ledger WHERE ts >= ? ORDER BY id ASC", (since,)))
            return list(self.conn.execute("SELECT * FROM ledger ORDER BY id ASC"))

    def ledger_balances(self, since: str | None = None) -> dict[str, float]:
        where = " WHERE ts >= ?" if since else ""
        params: tuple[Any, ...] = (since, since) if since else ()
        query = f"""
            SELECT account, SUM(signed_amount) AS amount
            FROM (
              SELECT debit AS account, amount AS signed_amount FROM ledger{where}
              UNION ALL
              SELECT credit AS account, -amount AS signed_amount FROM ledger{where}
            )
            GROUP BY account
        """
        with self._lock:
            rows = self.conn.execute(query, params).fetchall()
        return {str(row["account"]): usd_amount(row["amount"]) for row in rows}

    def ledger_version(self) -> tuple[int, int]:
        """Version token that notices same-store and external-connection writes."""
        with self._lock:
            data_version = int(self.conn.execute("PRAGMA data_version").fetchone()[0])
            return self._ledger_revision, data_version

    def set_kv(self, k: str, v: Any) -> None:
        self._execute_write(
            "INSERT INTO kv(k, v) VALUES(?, ?) ON CONFLICT(k) DO UPDATE SET v=excluded.v",
            (k, json.dumps(v)),
        )

    def get_kv(self, k: str, default: Any = None) -> Any:
        with self._lock:
            row = self.conn.execute("SELECT v FROM kv WHERE k=?", (k,)).fetchone()
        if not row:
            return default
        return json.loads(row["v"])

    def upsert_job(self, job: dict[str, Any]) -> None:
        record = dict(job)
        record["price_usd"] = usd_amount(record.get("price_usd", 0))
        self._execute_write(
            """
            INSERT INTO jobs(id, source, title, status, price_usd, payload)
            VALUES(:id, :source, :title, :status, :price_usd, :payload)
            ON CONFLICT(id) DO UPDATE SET
              status=excluded.status,
              price_usd=excluded.price_usd,
              payload=excluded.payload
            """,
            {
                "id": record["id"],
                "source": record.get("source", "unknown"),
                "title": record.get("title", ""),
                "status": record.get("status", "open"),
                "price_usd": record["price_usd"],
                "payload": json.dumps(record),
            },
        )

    def jobs(self, status: str | None = None) -> list[dict[str, Any]]:
        with self._lock:
            if status:
                rows = self.conn.execute(
                    "SELECT payload FROM jobs WHERE status=?", (status,)
                ).fetchall()
            else:
                rows = self.conn.execute("SELECT payload FROM jobs").fetchall()
        return [json.loads(r["payload"]) for r in rows]

    def get_job(self, job_id: str) -> dict[str, Any] | None:
        with self._lock:
            row = self.conn.execute("SELECT payload FROM jobs WHERE id=?", (job_id,)).fetchone()
        return json.loads(row["payload"]) if row else None

    def job_counts(self) -> dict[str, int]:
        with self._lock:
            rows = self.conn.execute("SELECT status, COUNT(*) AS n FROM jobs GROUP BY status").fetchall()
        return {r["status"]: int(r["n"]) for r in rows}

    def upsert_invoice(self, inv: dict[str, Any]) -> None:
        record = dict(inv)
        record["amount"] = usd_amount(record["amount"])
        self._execute_write(
            """
            INSERT INTO invoices(id, ts, job_id, amount, status, income_account, payload)
            VALUES(:id, :ts, :job_id, :amount, :status, :income_account, :payload)
            ON CONFLICT(id) DO UPDATE SET
              amount=excluded.amount,
              status=excluded.status,
              income_account=excluded.income_account,
              payload=excluded.payload
            """,
            {
                "id": record["id"],
                "ts": record.get("ts", iso()),
                "job_id": record["job_id"],
                "amount": record["amount"],
                "status": record["status"],
                "income_account": record.get("income_account", "income.labor"),
                "payload": json.dumps(record),
            },
        )

    def invoices(self, status: str | None = None) -> list[dict[str, Any]]:
        with self._lock:
            if status:
                rows = self.conn.execute(
                    "SELECT payload FROM invoices WHERE status=?", (status,)
                ).fetchall()
            else:
                rows = self.conn.execute("SELECT payload FROM invoices ORDER BY ts ASC").fetchall()
        return [json.loads(r["payload"]) for r in rows]

    def get_invoice(self, invoice_id: str) -> dict[str, Any] | None:
        with self._lock:
            row = self.conn.execute("SELECT payload FROM invoices WHERE id=?", (invoice_id,)).fetchone()
        return json.loads(row["payload"]) if row else None

    def invoice_for_job(self, job_id: str) -> dict[str, Any] | None:
        with self._lock:
            row = self.conn.execute(
                "SELECT payload FROM invoices WHERE job_id=? ORDER BY ts DESC LIMIT 1",
                (job_id,),
            ).fetchone()
        return json.loads(row["payload"]) if row else None

    def upsert_mail(self, msg: dict[str, Any]) -> None:
        self._execute_write(
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
        with self._lock:
            rows = self.conn.execute(q, args).fetchall()
        return [json.loads(r["payload"]) for r in rows]

    def upsert_offer(self, offer: dict[str, Any]) -> None:
        record = dict(offer)
        record["price_usd"] = usd_amount(record.get("price_usd", 0))
        self._execute_write(
            """
            INSERT INTO offers(id, ts, title, kind, price_usd, status, payload)
            VALUES(:id, :ts, :title, :kind, :price_usd, :status, :payload)
            ON CONFLICT(id) DO UPDATE SET
              status=excluded.status,
              payload=excluded.payload
            """,
            {
                "id": record["id"],
                "ts": record.get("ts", iso()),
                "title": record["title"],
                "kind": record.get("kind", "fixed"),
                "price_usd": record["price_usd"],
                "status": record.get("status", "listed"),
                "payload": json.dumps(record),
            },
        )

    def offers(self, status: str | None = None) -> list[dict[str, Any]]:
        with self._lock:
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
        self._execute_write(
            "INSERT INTO outcomes(ts, play_id, agent, kind, usd, success, note) VALUES (?,?,?,?,?,?,?)",
            (iso(), play_id, agent, kind, usd_amount(usd), 1 if success else 0, note),
        )

    def outcomes(self, limit: int = 100) -> list[dict[str, Any]]:
        with self._lock:
            rows = self.conn.execute(
                "SELECT * FROM outcomes ORDER BY id DESC LIMIT ?", (limit,)
            ).fetchall()
        return [dict(r) for r in rows]

    def insert_vote(self, action_id: str, agent: str, choice: str, reason: str) -> None:
        self._execute_write(
            "INSERT INTO votes(ts, action_id, agent, choice, reason) VALUES (?,?,?,?,?)",
            (iso(), action_id, agent, choice, reason),
        )

    def upsert_mission(self, mission: dict[str, Any]) -> None:
        record = dict(mission)
        record["budget_usd"] = usd_amount(record.get("budget_usd", 0))
        self._execute_write(
            """
            INSERT INTO missions(id, play_id, agent, title, status, budget_usd, created_ts, payload)
            VALUES(:id, :play_id, :agent, :title, :status, :budget_usd, :created_ts, :payload)
            ON CONFLICT(id) DO UPDATE SET
              status=excluded.status,
              payload=excluded.payload
            """,
            {
                "id": record["id"],
                "play_id": record["play_id"],
                "agent": record["agent"],
                "title": record["title"],
                "status": record["status"],
                "budget_usd": record["budget_usd"],
                "created_ts": record.get("created_ts", iso()),
                "payload": json.dumps(record),
            },
        )

    def missions(self, status: str | None = None) -> list[dict[str, Any]]:
        with self._lock:
            if status:
                rows = self.conn.execute(
                    "SELECT payload FROM missions WHERE status=?", (status,)
                ).fetchall()
            else:
                rows = self.conn.execute("SELECT payload FROM missions").fetchall()
        return [json.loads(r["payload"]) for r in rows]

    def close(self) -> None:
        with self._lock:
            self.conn.close()
