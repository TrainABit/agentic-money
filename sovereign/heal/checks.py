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


def diagnose(world: "World") -> list[Finding]:
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

    try:
        row = world.store.conn.execute("PRAGMA quick_check").fetchone()
        q = str(row[0]) if row else "unknown"
        out.append(Finding("sqlite", q == "ok", q, repairable=False))
    except Exception as e:
        out.append(Finding("sqlite", False, str(e)[:200], repairable=False))

    needed = {"events", "ledger", "missions", "jobs", "votes", "outcomes", "kv", "invoices", "mail", "offers"}
    have = {r[0] for r in world.store.conn.execute("SELECT name FROM sqlite_master WHERE type='table'")}
    miss_t = sorted(needed - have)
    out.append(Finding(
        "schema",
        ok=not miss_t,
        detail="ok" if not miss_t else f"missing tables {miss_t}",
        repairable=bool(miss_t),
        repair="migrate",
    ))

    try:
        pub = world.wallet.public()
        ok = pub["eth_address"].startswith("0x") and bool(pub["sol_address"])
        out.append(Finding("wallet", ok, pub["eth_address"] if ok else "missing", repairable=not ok, repair="wallet"))
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

    rec = world.store.get_kv("usdc_onchain")
    _ = rec
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

    return out


def _stale_lock(path: Path) -> int | None:
    if not path.exists():
        return None
    try:
        pid = int(path.read_text().strip() or "0")
    except Exception:
        return 0
    if pid <= 0:
        return 0
    try:
        import os
        os.kill(pid, 0)
        return None  # process lives
    except OSError:
        return pid
    except Exception:
        return pid
