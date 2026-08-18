from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Any, TYPE_CHECKING

if TYPE_CHECKING:
    from sovereign.engine.world import World


@dataclass
class Finding:
    code: str
    ok: bool
    detail: str
    repairable: bool = False
    repair: str | None = None

    def as_dict(self) -> dict[str, Any]:
        return {
            "code": self.code,
            "ok": self.ok,
            "detail": self.detail,
            "repairable": self.repairable,
            "repair": self.repair,
        }


def diagnose(world: "World", deep: bool = False) -> list[Finding]:
    """Engine health findings. ``deep`` adds the SQLite ``PRAGMA quick_check``
    integrity scan, which is too slow for every cheap tick; the schema/table
    checks always run."""
    paths = world.config.paths()
    out: list[Finding] = []

    missing = [str(p) for p in (
        paths.root, paths.logs, paths.playbooks, paths.work, paths.deliveries,
        paths.artifacts, paths.mail_outbox, paths.mail_inbox, paths.invoices,
    ) if not p.exists()]
    out.append(Finding(
        "paths",
        ok=not missing,
        detail="ok" if not missing else f"missing {missing}",
        repairable=bool(missing),
        repair="ensure_paths",
    ))

    if deep:
        try:
            row = world.store.conn.execute("PRAGMA quick_check").fetchone()
            q = str(row[0]) if row else "unknown"
            out.append(Finding("sqlite", q == "ok", q, repairable=False))
        except Exception as e:
            out.append(Finding("sqlite", False, str(e)[:200], repairable=False))

    needed = {
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
    have = {r[0] for r in world.store.conn.execute("SELECT name FROM sqlite_master WHERE type='table'")}
    miss_t = sorted(needed - have)
    out.append(Finding(
        "schema",
        ok=not miss_t,
        detail="ok" if not miss_t else f"missing tables {miss_t}",
        repairable=bool(miss_t),
        repair="migrate",
    ))
    from sovereign.memory.store import CURRENT_SCHEMA_VERSION

    version = world.store.schema_version()
    out.append(
        Finding(
            "schema_version",
            ok=version >= CURRENT_SCHEMA_VERSION,
            detail=f"user_version={version} current={CURRENT_SCHEMA_VERSION}",
            repairable=version < CURRENT_SCHEMA_VERSION,
            repair="migrate",
        )
    )

    try:
        pub = world.wallet.public()
        ok = pub["eth_address"].startswith("0x") and bool(pub["sol_address"])
        out.append(Finding("wallet", ok, pub["eth_address"] if ok else "missing", repairable=not ok, repair="wallet"))
        from sovereign.capital.wallet import derive_solana_keypair

        bundle = world.wallet.bundle
        mnemonic_sol = derive_solana_keypair(bundle.mnemonic)[0] if bundle else ""
        backup_ok = bool(bundle) and mnemonic_sol == pub["sol_address"]
        out.append(
            Finding(
                "wallet_backup",
                backup_ok,
                "mnemonic restores ETH and SOL"
                if backup_ok
                else "legacy SOL key requires secrets.enc backup",
                repairable=False,
            )
        )
        backend = getattr(getattr(world.config, "wallet", None), "master_key_backend", "file")
        has_file = paths.master_key.exists()
        if backend == "keyring":
            custody_detail = "keyring backend"
            if has_file:
                custody_detail += " (master.key file still present; safe to remove after verify)"
        else:
            custody_detail = (
                "file backend; master.key present" if has_file else "file backend; master.key missing"
            )
        out.append(
            Finding(
                "master_key_custody",
                ok=backend == "keyring" or has_file,
                detail=custody_detail,
                repairable=False,
            )
        )
    except Exception as e:
        out.append(Finding("wallet", False, str(e)[:200], repairable=True, repair="wallet"))

    from sovereign.memory.playbooks import DEFAULT_PLAYBOOKS

    missing_pb = [n for n in DEFAULT_PLAYBOOKS if not (paths.playbooks / f"{n}.md").exists()]
    out.append(Finding(
        "playbooks",
        ok=not missing_pb,
        detail="ok" if not missing_pb else f"missing {missing_pb}",
        repairable=bool(missing_pb),
        repair="seed_playbooks",
    ))

    inbox_ok = True
    inbox_detail = "ok"
    if paths.human.exists():
        try:
            import json
            json.loads(paths.human.read_text())
        except Exception as e:
            inbox_ok = False
            inbox_detail = f"corrupt: {e}"[:160]
    else:
        inbox_ok = False
        inbox_detail = "missing"
    out.append(Finding("human_inbox", inbox_ok, inbox_detail, repairable=not inbox_ok, repair="human_inbox"))

    stale = _stale_lock(paths.lock)
    out.append(Finding(
        "lock",
        ok=not stale,
        detail="ok" if not stale else f"stale lock pid={stale}",
        repairable=bool(stale),
        repair="stale_lock",
    ))

    cert = [c for c in world.certified if c.get("certified")]
    out.append(Finding(
        "strategies",
        ok=bool(cert) or world.tick == 0,
        detail=f"{len(cert)} certified" if cert else ("pending first tick" if world.tick == 0 else "none certified"),
        repairable=not cert,
        repair="recertify",
    ))

    tools_ok = world.tools is not None
    out.append(Finding(
        "tools",
        ok=tools_ok,
        detail="bound" if tools_ok else "unbound",
        repairable=not tools_ok,
        repair="bind_tools",
    ))

    payment = world.store.get_kv("payment_watch_v2") or {}
    suspense = float(world.store.get_kv("usdc_suspense", 0.0) or 0.0)
    reserved = float(world.store.get_kv("usdc_manual_reserved", 0.0) or 0.0)
    payment_ok = not payment or (
        payment.get("version") == 2 and suspense < 0.000001 and reserved < 0.000001
    )
    out.append(
        Finding(
            "payment_reconciliation",
            payment_ok,
            (
                "ok"
                if payment_ok
                else f"suspense={suspense:.6f} reserved={reserved:.6f}"
            ),
            repairable=False,
        )
    )
    book = world.treasury.trading_book()
    drift = world.broker.cash == 0 and book > 0
    out.append(Finding(
        "broker_sync",
        ok=not drift,
        detail="ok" if not drift else f"broker cash 0 but book {book}",
        repairable=drift,
        repair="sync_broker",
    ))

    open_inv = world.store.invoices("open")
    recv = world.ledger.balance("assets.receivable")
    # receivable should roughly match open invoices
    inv_sum = round(sum(float(i.get("amount") or 0) for i in open_inv), 2)
    drift_inv = abs(recv - inv_sum) > 1.0 and (recv > 0 or inv_sum > 0)
    out.append(Finding(
        "receivable",
        ok=not drift_inv,
        detail=f"receivable={recv} open_invoices={inv_sum}",
        repairable=False,
    ))

    comms = getattr(world, "comms", None)
    if comms is None:
        out.append(Finding("comms", True, "not wired", repairable=False))
    else:
        counts = comms.counts()
        dead = int(counts.get("dead", 0))
        out.append(Finding(
            "comms",
            ok=dead == 0,
            detail=f"counts={counts}",
            repairable=False,
        ))

    # Informational only: web automation is opt-in, so its state can never
    # fail health or trigger a repair.
    from sovereign.ops import _mcp_summary, _web_summary

    web = _web_summary(world)
    out.append(Finding(
        "web",
        ok=True,
        detail=(
            f"enabled={web['enabled']} playwright={web['playwright']} "
            f"vaulted_sessions={web['vaulted_sessions']}"
        ),
        repairable=False,
    ))

    # Informational only, and built from config plus cached registry state —
    # diagnosing must never connect to (or spawn) an MCP server.
    mcp = _mcp_summary(world)
    out.append(Finding(
        "mcp",
        ok=True,
        detail=(
            f"enabled={mcp['enabled']} sdk={mcp['sdk']} "
            f"servers={mcp['servers']} errors={mcp['errors']}"
        ),
        repairable=False,
    ))

    return out


def _stale_lock(path: Path) -> int | None:
    if not path.exists():
        return None
    # The kernel lock is authoritative. PID text can be stale, truncated, or
    # refer to a reused process, so only repair an inode that is not locked.
    import fcntl

    try:
        with path.open("a+") as handle:
            try:
                fcntl.flock(handle, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except BlockingIOError:
                return None
            try:
                handle.seek(0)
                raw = handle.read().strip()
                return int(raw) if raw.isdigit() and int(raw) > 0 else -1
            finally:
                fcntl.flock(handle, fcntl.LOCK_UN)
    except OSError:
        return None
