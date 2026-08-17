from __future__ import annotations

import hashlib
from typing import Any, TYPE_CHECKING

from sovereign.memory.store import iso

if TYPE_CHECKING:
    from sovereign.engine.world import World


def _inv_id(job_id: str) -> str:
    return "inv_" + hashlib.sha1(job_id.encode()).hexdigest()[:10]


def quote_usd(job: dict[str, Any]) -> float:
    priced = float(job.get("price_usd") or 0)
    if priced > 0:
        return priced
    fit = float(job.get("fit") or 0.5)
    return round(450 + fit * 900, 2)


def issue(world: "World", job: dict[str, Any], income_account: str = "income.labor") -> dict[str, Any]:
    existing = world.store.invoice_for_job(job["id"])
    if existing and existing.get("status") in {"open", "paid"}:
        return existing
    amount = quote_usd(job)
    pub = world.wallet.public()
    inv = {
        "id": _inv_id(job["id"]),
        "ts": world.stamp(),
        "job_id": job["id"],
        "title": job.get("title", ""),
        "amount": amount,
        "asset": "USDC",
        "status": "open",
        "income_account": income_account,
        "eth_address": pub["eth_address"],
        "sol_address": pub["sol_address"],
        "memo": f"SOV-{job['id'][-8:].upper()}",
        "issued_tick": world.tick,
    }
    world.store.upsert_invoice(inv)
    job["status"] = "invoiced"
    job["invoice_id"] = inv["id"]
    job["price_usd"] = amount
    world.store.upsert_job(job)
    # billed, not earned
    world.ledger.post(
        "assets.receivable",
        "liability.unearned",
        amount,
        f"invoice {inv['id']}",
        ref=inv["id"],
        ts=world.stamp(),
    )
    path = world.config.paths().invoices / f"{inv['id']}.md"
    path.write_text(_markdown(inv, world.config.firm_name))
    inv["path"] = str(path)
    world.store.upsert_invoice(inv)
    return inv


def collect(
    world: "World",
    invoice_or_job: str,
    source: str = "manual",
) -> dict[str, Any]:
    inv = world.store.get_invoice(invoice_or_job)
    if not inv:
        inv = world.store.invoice_for_job(invoice_or_job)
    if not inv:
        raise KeyError(invoice_or_job)
    if inv.get("status") in {"paid", "void"}:
        return inv
    amount = float(inv["amount"])
    account = inv.get("income_account") or "income.labor"
    world.ledger.post(
        "assets.usdc",
        "assets.receivable",
        amount,
        f"collect {inv['id']} via {source}",
        ref=inv["id"],
        ts=world.stamp(),
    )
    world.ledger.post(
        "liability.unearned",
        account,
        amount,
        f"recognize {inv['id']}",
        ref=inv["id"],
        ts=world.stamp(),
    )
    inv["status"] = "paid"
    inv["paid_ts"] = world.stamp()
    inv["paid_source"] = source
    world.store.upsert_invoice(inv)
    job = world.store.get_job(inv["job_id"])
    if job:
        job["status"] = "paid"
        world.store.upsert_job(job)
        play = {
            "income.labor": "labor_studio",
            "income.products": "digital_products",
            "income.retainers": "productized",
            "income.trading": "tsmom_crypto",
        }.get(account, "labor_studio")
        world.store.outcome("collect", amount, True, job.get("title", ""), "treasurer", play)
    return inv


def void(world: "World", invoice_or_job: str, reason: str = "aged") -> dict[str, Any]:
    inv = world.store.get_invoice(invoice_or_job) or world.store.invoice_for_job(invoice_or_job)
    if not inv:
        raise KeyError(invoice_or_job)
    if inv.get("status") != "open":
        return inv
    amount = float(inv["amount"])
    world.ledger.post(
        "liability.unearned",
        "assets.receivable",
        amount,
        f"void {inv['id']} {reason}",
        ref=inv["id"],
        ts=world.stamp(),
    )
    inv["status"] = "void"
    inv["void_reason"] = reason
    world.store.upsert_invoice(inv)
    return inv


def _markdown(inv: dict[str, Any], firm: str) -> str:
    return (
        f"# Invoice {inv['id']}\n\n"
        f"**From:** {firm}\n"
        f"**For:** {inv.get('title')} (`{inv['job_id']}`)\n"
        f"**Amount:** ${inv['amount']:.2f} USDC\n"
        f"**Memo:** `{inv['memo']}`\n\n"
        f"## Pay\n"
        f"- Ethereum USDC: `{inv['eth_address']}`\n"
        f"- Solana USDC: `{inv['sol_address']}`\n\n"
        f"Send the exact amount with the memo in a note if the rail supports it. "
        f"The engine watches ETH and Solana USDC and marks this paid on receipt. "
        f"Email text never settles an invoice.\n"
    )
