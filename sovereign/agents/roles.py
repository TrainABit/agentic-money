from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import numpy as np

from sovereign.capital import invoice as invoices
from sovereign.capital.invoice import quote_usd
from sovereign.capital.payments import watch_and_collect
from sovereign.channels import mail as mailbox
from sovereign.engine.heartbeat import fund_missions, playbook
from sovereign.engine.world import World
from sovereign.labor.boards import proposal_text, sim_client_accepts
from sovereign.labor.craft import produce
from sovereign.labor.pipeline import accept_job
from sovereign.markets.strategies import STRATEGIES
from sovereign.plays import PLAYS, attention_map, play_roi


def bookkeeper(world: World) -> list[dict[str, Any]]:
    snap = world.ledger.snapshot(now=world.now)
    world.store.set_kv("last_snapshot", snap)
    return [
        {
            "kind": "snapshot",
            "equity": snap["equity_usd"],
            "revenue": snap["revenue_usd"],
            "trailing_30d": snap["trailing_30d_usd"],
        }
    ]


def risk(world: World) -> list[dict[str, Any]]:
    out: list[dict[str, Any]] = []
    halt = world.broker.maybe_halt(world.config.risk)
    if halt:
        world.frozen.add("trader")
        out.append({"kind": "circuit_break", "reason": halt, "broker": world.broker.snapshot()})
    for agent, score in list(world.reputation.scores.items()):
        if score < 20:
            world.frozen.add(agent)
            out.append({"kind": "rep_freeze", "agent": agent, "score": score})
    if world.config.risk.operating_cash_is_tradable:
        world.config.risk.operating_cash_is_tradable = False
        out.append({"kind": "policy", "rule": "operating_cash_walled"})
    return out


def ethics(world: World) -> list[dict[str, Any]]:
    notes = []
    recent = world.store.events(30)
    for ev in recent:
        blob = json.dumps(ev).lower()
        if any(s in blob for s in ("mnemonic", "sol_secret", "eth_key", "smtp_pass")):
            world.reputation.slash("operator", 50, "secret leakage")
            world.frozen.add("operator")
            notes.append("secret_leak")
    applies = len(world.store.jobs("applied"))
    accepts = len(world.store.jobs("accepted")) + len(world.store.jobs("paid")) + len(world.store.jobs("invoiced"))
    if applies >= 12 and accepts == 0:
        world.reputation.slash("closer", 8, "spray without accepts")
        notes.append("spray")
    for msg in world.store.mail(direction="out")[:20]:
        body = (msg.get("body") or "").lower()
        if "guaranteed profit" in body or "risk-free" in body:
            world.reputation.slash("closer", 10, "prohibited claim")
            notes.append("prohibited_claim")
    return [{"kind": "ethics", "notes": notes}]


def director(world: World) -> list[dict[str, Any]]:
    created = fund_missions(world)
    snap = world.ledger.snapshot(now=world.now)
    goals = world.config.goals
    gap = max(0.0, goals.minimum_usd - snap["trailing_30d_usd"])
    roi = play_roi(world.store.outcomes(80))
    note = "hold labor-first mix" if gap > 0 else "shift toward retainers/products"
    if world.tick % 20 == 0 and world.config.mode == "live":
        world.router.complete(
            f"Weekly council. Trailing={snap['trailing_30d_usd']} gap_to_min={gap}. {note}. roi={roi}",
            tier="think",
            system=playbook(world, "director"),
        )
    world.store.set_kv("attention", attention_map(snap["trailing_30d_usd"], goals.minimum_usd, goals.recommended_usd, roi=roi))
    return [{"kind": "direct", "missions_opened": len(created), "gap_to_min": gap, "note": note, "roi": roi}]


def hunter(world: World) -> list[dict[str, Any]]:
    live = world.config.mode == "live" and world.config.public_job_apis
    found = world.board.search(tick=world.tick, live=live, include_sim=world.config.mode == "sim")
    taken = 0
    picked = []
    existing = {j["id"] for j in world.store.jobs()}
    for job in found:
        if job["id"] in existing:
            continue
        if job.get("fit", 0) < 0.45:
            continue
        job["status"] = "open"
        if not job.get("price_usd"):
            job["price_usd"] = quote_usd(job)
        world.store.upsert_job(job)
        picked.append(job["title"])
        taken += 1
        if taken >= 4:
            break
    if live and taken == 0:
        world.human.ask(
            "job-platforms",
            "Optional: Upwork/Fiverr tokens. Public boards already run. Or drop inbound leads as JSON in data/mail/inbox/.",
            ["note"],
            "Extra channels. Not required to earn.",
        )
    return [{"kind": "hunt", "new": taken, "titles": picked}]


def closer(world: World) -> list[dict[str, Any]]:
    open_jobs = [j for j in world.store.jobs("open")]
    open_jobs.sort(key=lambda j: j.get("fit", 0), reverse=True)
    results = []
    cap = world.config.daily_apply_cap
    already = len(world.store.jobs("applied"))
    budget = min(3 if world.config.mode == "sim" else 2, max(0, cap - already))
    rate = world.config.sim.close_rate if world.config.mode == "sim" else 1.0
    for job in open_jobs[:budget]:
        job["price_usd"] = quote_usd(job)
        blurb = world.router.complete(
            f"Write a short proposal for: {job.get('title')}\n{job.get('description','')}\n"
            f"Playbook:\n{playbook(world, 'closer')}",
            tier="work",
        )
        text = proposal_text(job, world.config.firm_name, blurb)
        job["proposal"] = text
        to = job.get("contact") or job.get("email") or "client@unknown.local"
        mailbox.send(
            world,
            to=to,
            subject=f"Proposal: {job.get('title')} [{job['id']}]",
            body=text,
            job_id=job["id"],
            kind="proposal",
        )
        if world.config.auto_accept():
            if sim_client_accepts(job, text, close_rate=rate):
                accept_job(world, job["id"], source="sim")
                results.append({"id": job["id"], "status": "accepted", "price": job.get("price_usd")})
            else:
                job["status"] = "rejected"
                world.store.upsert_job(job)
                world.store.outcome("proposal", 0, False, job["title"], "closer", "labor_studio")
                results.append({"id": job["id"], "status": "rejected"})
        else:
            job["status"] = "applied"
            world.store.upsert_job(job)
            results.append({"id": job["id"], "status": "applied", "price": job.get("price_usd")})
    return [{"kind": "close", "results": results}]


def crafter(world: World) -> list[dict[str, Any]]:
    queue = world.store.jobs("accepted") + world.store.jobs("in_progress")
    if not queue:
        return [{"kind": "craft", "did": 0}]
    job = queue[0]
    job["status"] = "in_progress"
    world.store.upsert_job(job)
    artifact = produce(world, job)
    job["status"] = "delivered"
    job["delivery_path"] = artifact["delivery"]
    job["entry"] = artifact.get("entry")
    job["files"] = artifact.get("files")
    world.store.upsert_job(job)
    world.store.outcome("delivery", float(job.get("price_usd") or 0), True, job["title"], "crafter", "labor_studio")
    world.reputation.boost("crafter", 2.0, "delivered")
    mailbox.send(
        world,
        to=job.get("contact") or "client@unknown.local",
        subject=f"Delivery: {job.get('title')} [{job['id']}]",
        body=f"Files are ready. Entry: {artifact.get('entry')}\nInvoice follows.",
        job_id=job["id"],
        kind="delivery",
    )
    return [{"kind": "craft", "job": job["id"], "path": artifact["delivery"], "files": artifact.get("files")}]


def treasurer(world: World) -> list[dict[str, Any]]:
    issued = []
    for job in world.store.jobs("delivered"):
        income = "income.products" if job.get("source") == "product" else "income.labor"
        if "retainer" in str(job.get("title", "")).lower():
            income = "income.retainers"
        inv = invoices.issue(world, job, income_account=income)
        issued.append(inv["id"])
        mailbox.send(
            world,
            to=job.get("contact") or "client@unknown.local",
            subject=f"Invoice {inv['id']} — ${inv['amount']:.0f} USDC [{job['id']}]",
            body=f"Pay ${inv['amount']:.2f} USDC to {inv['eth_address']} memo {inv['memo']}",
            job_id=job["id"],
            kind="invoice",
        )

    collected = []
    if world.config.autocollect():
        delay = world.config.sim.pay_delay_ticks
        for inv in world.store.invoices("open"):
            issued_tick = int(inv.get("issued_tick") or 0)
            if world.tick - issued_tick >= delay:
                collected.append(invoices.collect(world, inv["id"], source="autocollect"))
    else:
        collected.extend(watch_and_collect(world))

    snap = world.ledger.snapshot(now=world.now)
    if (
        snap["trailing_30d_usd"] >= 800
        and world.treasury.trading_book() < 50
        and world.treasury.operating_cash() > 200
    ):
        alloc = min(80.0, world.treasury.operating_cash() * 0.08)
        ok = world.treasury.allocate_trading(alloc, "seed trading book")
        if ok and world.broker.cash <= 0:
            world.broker.cash = alloc

    if world.broker.cash == 0 and world.treasury.trading_book() > 0:
        world.broker.cash = world.treasury.trading_book()

    return [
        {
            "kind": "treasury",
            "issued": issued,
            "collected": [c.get("id") for c in collected],
            "policy": world.treasury.policy_status(),
        }
    ]


def trader(world: World) -> list[dict[str, Any]]:
    certified = [c for c in world.certified if c.get("certified")]
    if not certified:
        return [{"kind": "trade", "skipped": "none_certified"}]
    if world.broker.frozen or "trader" in world.frozen:
        return [{"kind": "trade", "skipped": "frozen"}]
    if world.treasury.trading_book() <= 0 and world.broker.cash <= 0:
        return [{"kind": "trade", "skipped": "no_book"}]

    sid = certified[0]["strategy_id"]
    strat = STRATEGIES[sid]
    close = np.array(world.market_close, dtype=float)
    idx = min(len(close) - 1, max(strat.__dict__.get("lookback", 50) + 5, 60 + world.tick))
    price = float(close[idx])
    world.broker.mark(price)
    pos = strat.positions(close[: idx + 1])
    desired_frac = float(pos[-1])
    book = max(world.broker.equity(), world.treasury.trading_book(), 1.0)
    desired_notional = desired_frac * book * 0.5
    fill = world.broker.target_position(desired_notional, price, world.config.risk.round_trip_cost)
    eq = world.broker.equity()
    prev = world.store.get_kv("trader_last_eq", eq)
    delta = eq - float(prev)
    world.store.set_kv("trader_last_eq", eq)
    if abs(delta) >= 0.01:
        if delta > 0:
            world.ledger.post("assets.trading_book", "income.trading", delta, "mtm gain", ts=world.stamp())
        else:
            world.ledger.post("income.trading", "assets.trading_book", -delta, "mtm loss", ts=world.stamp())
    return [{"kind": "trade", "strategy": sid, "price": price, "fill": fill, "equity": eq}]


def publisher(world: World) -> list[dict[str, Any]]:
    if world.tick % 8 != 0:
        return [{"kind": "publish", "skipped": True}]
    deliveries = [p for p in world.config.paths().deliveries.rglob("*") if p.is_file()]
    if not deliveries:
        return [{"kind": "publish", "skipped": "no_deliveries"}]
    latest = max(deliveries, key=lambda p: p.stat().st_mtime)
    listing = world.config.paths().artifacts / "product_listing.md"
    copy = world.router.complete(
        f"Write a 1-page product listing based on this delivery:\n{latest.read_text(errors='ignore')[:1500]}",
        tier="work",
    )
    listing.write_text(
        f"# Product listing — {world.config.firm_name}\n\n{copy}\n\nPay USDC to {world.identity.get('eth')}\n"
    )
    offer = {
        "id": "offer_ops_kit",
        "ts": world.stamp(),
        "title": "Packaged automation kit",
        "kind": "product",
        "price_usd": 49.0,
        "status": "listed",
        "path": str(listing),
    }
    world.store.upsert_offer(offer)
    if world.config.mode == "sim":
        pid = f"prod_{world.tick}"
        world.store.upsert_job(
            {
                "id": pid,
                "source": "product",
                "title": "Packaged automation kit",
                "status": "delivered",
                "price_usd": 49.0,
                "contact": "buyer@sim.local",
            }
        )
        world.store.outcome("product", 49.0, True, "kit", "publisher", "digital_products")
        return [{"kind": "publish", "listing": str(listing), "sim_sale": 49.0}]
    return [{"kind": "publish", "listing": str(listing)}]


def scout(world: World) -> list[dict[str, Any]]:
    catalog = [
        {
            "id": "offer_ops48",
            "title": "48h operations automation",
            "kind": "fixed",
            "price_usd": 1200,
            "status": "listed",
            "ts": world.stamp(),
        },
        {
            "id": "offer_inbox_retainer",
            "title": "Inbox SOP + weekly report bot",
            "kind": "retainer",
            "price_usd": 400,
            "setup_usd": 1500,
            "status": "listed",
            "ts": world.stamp(),
        },
    ]
    for o in catalog:
        world.store.upsert_offer(o)
    path = world.config.paths().artifacts / "offers.md"
    path.write_text(
        "# Offers\n\n"
        + "\n".join(f"- **{o['title']}** — ${o['price_usd']:.0f} ({o['kind']})" for o in catalog)
        + f"\n\nPay USDC `{world.identity.get('eth')}`\n"
    )
    if world.config.mode == "sim" and world.ledger.snapshot(now=world.now)["trailing_30d_usd"] >= 1500:
        if not any(j.get("id") == "retainer_inbox" for j in world.store.jobs()):
            world.store.upsert_job(
                {
                    "id": "retainer_inbox",
                    "source": "retainer",
                    "title": "Weekly competitor memo (retainer)",
                    "status": "delivered",
                    "price_usd": 400,
                    "contact": "retainer@sim.local",
                }
            )
    if world.tick % 10 != 0:
        return [{"kind": "scout", "offers": len(catalog)}]
    note = world.router.complete(
        f"Pick the better next experiment given trailing={world.ledger.snapshot(now=world.now)['trailing_30d_usd']} offers={catalog}",
        tier="fast",
    )
    return [{"kind": "scout", "ideas": catalog, "note": note}]


def operator(world: World) -> list[dict[str, Any]]:
    plan = {
        "provider": "hetzner",
        "spec": "cx22 2 vCPU / 4GB",
        "est_usd": 6.0,
        "why": "jail for crafter + dashboard",
    }
    token = world.wallet.get_credential("HETZNER_API_TOKEN")
    if not token and world.treasury.operating_cash() < 100:
        world.human.ask(
            "vps",
            "Optional: Hetzner API token. Engine buys the smallest box only after Treasurer+Director quorum.",
            ["HETZNER_API_TOKEN"],
            "Compute. Local process is enough to earn labor.",
        )
        return [{"kind": "ops", "planned": plan, "bought": False}]
    votes = world.council.auto_votes_for_spend(
        6.0,
        world.treasury.operating_cash(),
        frozen="operator" in world.frozen,
        autonomy=world.reputation.autonomy_usd("operator", 80),
    )
    votes["director"] = "yes" if world.ledger.snapshot(now=world.now)["trailing_30d_usd"] > 0 else "no"
    ok, reason = world.council.quorum(f"vps_{world.tick}", "buy_infra", votes)
    bought = False
    if ok and not world.store.get_kv("vps_bought"):
        if world.config.mode == "sim" or (token and world.config.allow_live_infra_buy):
            if world.treasury.pay(6.0, "expenses.infra", "vps month", ts=world.stamp()):
                world.store.set_kv("vps_bought", True)
                bought = True
    return [{"kind": "ops", "planned": plan, "votes": votes, "bought": bought, "reason": reason, "has_token": bool(token)}]


def auditor(world: World) -> list[dict[str, Any]]:
    notes = []
    delivered = [
        j
        for j in world.store.jobs("delivered") + world.store.jobs("invoiced") + world.store.jobs("paid")
        if j.get("delivery_path")
    ]
    if delivered:
        last = delivered[-1]
        path = last.get("delivery_path")
        ok = False
        if path:
            p = Path(path)
            ok = p.exists() and (
                (p.is_file() and p.stat().st_size > 80)
                or (p.is_dir() and any(p.iterdir()))
            )
        if ok:
            world.reputation.boost("crafter", 0.5, "audit pass")
            notes.append({"job": last["id"], "pass": True})
        else:
            world.reputation.slash("crafter", 4, "empty delivery")
            notes.append({"job": last["id"], "pass": False})
    if world.broker.frozen:
        notes.append({"broker": "halted", "pass": True})
    live_uncert = any(
        e.get("kind") == "trade" and (e.get("payload") or {}).get("strategy")
        for e in world.store.events(5)
    )
    _ = live_uncert
    verdict = world.router.complete(
        f"Audit notes: {notes}. Slash spray or uncertified live trades.",
        tier="fast",
        system="Return a one-line verdict.",
    )
    return [{"kind": "audit", "notes": notes, "verdict": verdict}]


def improver(world: World) -> list[dict[str, Any]]:
    if world.tick % 7 != 0:
        return []
    outcomes = world.store.outcomes(40)
    wins = sum(1 for o in outcomes if o["success"])
    n = len(outcomes) or 1
    roi = play_roi(outcomes)
    patch = world.router.complete(
        f"Outcomes winrate={wins}/{n} roi={roi}. Write a playbook patch for closer and hunter.",
        tier="work",
        system=playbook(world, "improver"),
    )
    trial = world.config.paths().playbooks / "closer.trial.md"
    trial.write_text(patch)
    promoted = False
    if wins / n >= 0.4:
        (world.config.paths().playbooks / "closer.md").write_text(
            playbook(world, "closer") + "\n\n## Improver patch\n" + patch + "\n"
        )
        promoted = True
        world.store.outcome("playbook", 0, True, "promoted closer patch", "improver", "labor_studio")
    # Starve zero-EV plays in override
    override = world.store.get_kv("attention_override") or {}
    for p in PLAYS:
        if roi.get(p.id, 0) <= 0 and world.tick > 14:
            override[p.id] = max(0.01, attention_map(0, 2000, 5000).get(p.id, 0.05) * 0.5)
    if override:
        world.store.set_kv("attention_override", override)
    return [{"kind": "improve", "winrate": round(wins / n, 3), "promoted": promoted, "roi": roi}]


def courier(world: World) -> list[dict[str, Any]]:
    if world.treasury.trading_book() > 0 and world.config.mode == "live":
        world.human.ask(
            "exchange",
            "KYC an exchange or stay on-chain. Spot API keys, withdraw disabled, IP allowlist.",
            ["EXCHANGE_API_KEY", "EXCHANGE_API_SECRET"],
            "Live execution. Paper runs without this.",
        )
    if world.config.mode == "live" and not world.wallet.get_credential("SMTP_HOST"):
        world.human.ask(
            "smtp",
            "Optional SMTP so proposals leave the outbox. Without it, mail is written to data/mail/ and still counted as sent_local.",
            ["SMTP_HOST", "SMTP_PORT", "SMTP_USER", "SMTP_PASS", "SMTP_FROM"],
            "Outbound email. File outbox already works.",
        )
    open_q = world.human.open()
    return [{"kind": "courier", "open_human_requests": len(open_q), "ids": [i["id"] for i in open_q]}]
