from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import numpy as np

from sovereign.engine.heartbeat import fund_missions, playbook
from sovereign.engine.world import World
from sovereign.labor.boards import deliverable_text, proposal_text, sim_client_accepts
from sovereign.markets.strategies import STRATEGIES


def bookkeeper(world: World) -> list[dict[str, Any]]:
    snap = world.ledger.snapshot()
    world.store.set_kv("last_snapshot", snap)
    return [{"kind": "snapshot", "equity": snap["equity_usd"], "revenue": snap["revenue_usd"]}]


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
    # Wall: operating cash is not tradable
    if world.config.risk.operating_cash_is_tradable:
        world.config.risk.operating_cash_is_tradable = False
        out.append({"kind": "policy", "rule": "operating_cash_walled"})
    return out


def director(world: World) -> list[dict[str, Any]]:
    created = fund_missions(world)
    snap = world.ledger.snapshot()
    goals = world.config.goals
    gap = max(0.0, goals.minimum_usd - snap["revenue_usd"])
    note = "hold labor-first mix" if gap > 0 else "shift toward retainers/products"
    if world.tick % 20 == 0 and world.config.mode == "live":
        world.router.complete(
            f"Weekly council. Revenue={snap['revenue_usd']} gap_to_min={gap}. {note}. "
            f"Certified={ [c['strategy_id'] for c in world.certified if c.get('certified')] }",
            tier="think",
            system=playbook(world, "director"),
        )
    return [{"kind": "direct", "missions_opened": len(created), "gap_to_min": gap, "note": note}]


def hunter(world: World) -> list[dict[str, Any]]:
    live = world.config.mode == "live" and world.config.public_job_apis
    found = world.board.search(tick=world.tick, live=live)
    taken = 0
    picked = []
    existing = {j["id"] for j in world.store.jobs()}
    for job in found:
        if job["id"] in existing:
            continue
        if job.get("fit", 0) < 0.45:
            continue
        job["status"] = "open"
        world.store.upsert_job(job)
        picked.append(job["title"])
        taken += 1
        if taken >= 4:
            break
    if live and not picked:
        world.human.ask(
            "job-platforms",
            "Optional: provide Upwork/Fiverr session cookies or API tokens if you want those boards. Public boards already run.",
            ["note"],
            "Extra channels. Not required to earn.",
        )
    return [{"kind": "hunt", "new": taken, "titles": picked}]


def closer(world: World) -> list[dict[str, Any]]:
    open_jobs = [j for j in world.store.jobs("open")]
    open_jobs.sort(key=lambda j: j.get("fit", 0), reverse=True)
    results = []
    budget = 3 if world.config.mode == "sim" else 2
    for job in open_jobs[:budget]:
        blurb = world.router.complete(
            f"Write a short proposal for: {job.get('title')}\n{job.get('description','')}\n"
            f"Playbook:\n{playbook(world, 'closer')}",
            tier="work",
        )
        text = proposal_text(job, world.config.firm_name, blurb)
        job["proposal"] = text
        accepted = False
        if world.config.mode == "sim":
            accepted = sim_client_accepts(job, text)
        else:
            # Live: we still send; acceptance arrives later via email/board. In-engine, mark applied.
            job["status"] = "applied"
            world.store.upsert_job(job)
            results.append({"id": job["id"], "status": "applied"})
            continue
        if accepted:
            job["status"] = "accepted"
            world.store.upsert_job(job)
            world.store.outcome("proposal", job.get("price_usd", 0), True, job["title"], "closer", "labor_studio")
            world.reputation.boost("closer", 1.5, "accepted")
            results.append({"id": job["id"], "status": "accepted", "price": job.get("price_usd")})
        else:
            job["status"] = "rejected"
            world.store.upsert_job(job)
            world.store.outcome("proposal", 0, False, job["title"], "closer", "labor_studio")
            results.append({"id": job["id"], "status": "rejected"})
    return [{"kind": "close", "results": results}]


def crafter(world: World) -> list[dict[str, Any]]:
    queue = world.store.jobs("accepted") + world.store.jobs("in_progress")
    if not queue:
        return [{"kind": "craft", "did": 0}]
    job = queue[0]
    job["status"] = "in_progress"
    world.store.upsert_job(job)
    workdir: Path = world.config.paths().work / job["id"]
    workdir.mkdir(parents=True, exist_ok=True)
    body = deliverable_text(job, world.config.firm_name)
    if world.config.mode == "live":
        extra = world.router.complete(
            f"Produce the deliverable for: {job.get('title')}\n{job.get('description')}\n"
            f"Write the main artifact in markdown/code.",
            tier="work",
            system=playbook(world, "crafter"),
        )
        body = body + "\n\n## Artifact\n\n" + extra
    (workdir / "DELIVERY.md").write_text(body)
    dest = world.config.paths().deliveries / f"{job['id']}.md"
    dest.write_text(body)
    job["status"] = "delivered"
    job["delivery_path"] = str(dest)
    world.store.upsert_job(job)
    world.store.outcome("delivery", job.get("price_usd", 0), True, job["title"], "crafter", "labor_studio")
    world.reputation.boost("crafter", 2.0, "delivered")
    return [{"kind": "craft", "job": job["id"], "path": str(dest), "price": job.get("price_usd")}]


def treasurer(world: World) -> list[dict[str, Any]]:
    collected = []
    for job in world.store.jobs("delivered"):
        price = float(job.get("price_usd") or 0)
        if price <= 0:
            # Live board jobs without a price: invoice placeholder
            job["status"] = "invoiced"
            job["invoice_usd"] = 0
            world.store.upsert_job(job)
            continue
        world.treasury.receive(price, f"job:{job['id']}", "income.labor", ref=job["id"])
        job["status"] = "paid"
        world.store.upsert_job(job)
        collected.append({"id": job["id"], "usd": price})
        world.store.outcome("collect", price, True, job["title"], "treasurer", "labor_studio")

    for job in world.store.jobs("product_sold"):
        price = float(job.get("price_usd") or 0)
        world.treasury.receive(price, f"product:{job['id']}", "income.products", ref=job["id"])
        job["status"] = "paid"
        world.store.upsert_job(job)
        collected.append({"id": job["id"], "usd": price, "kind": "product"})

    # After minimum-path cash, sprinkle a tiny certified trading allocation
    snap = world.ledger.snapshot()
    if (
        snap["revenue_usd"] >= 800
        and world.treasury.trading_book() < 50
        and world.treasury.operating_cash() > 200
    ):
        alloc = min(80.0, world.treasury.operating_cash() * 0.08)
        ok = world.treasury.allocate_trading(alloc, "seed trading book")
        if ok and world.broker.cash <= 0:
            world.broker.cash = alloc

    # Sync broker cash with trading book if broker empty but book exists
    if world.broker.cash == 0 and world.treasury.trading_book() > 0:
        world.broker.cash = world.treasury.trading_book()

    return [{"kind": "treasury", "collected": collected, "policy": world.treasury.policy_status()}]


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
    # Advance a cursor so paper trading walks the series across ticks
    idx = min(len(close) - 1, max(strat.__dict__.get("lookback", 50) + 5, 60 + world.tick))
    price = float(close[idx])
    world.broker.mark(price)
    pos = strat.positions(close[: idx + 1])
    desired_frac = float(pos[-1])  # -2..2 vol-targeted
    # Risk: 1% of book * signal, but vol targeting already sized. Cap by book.
    book = max(world.broker.equity(), world.treasury.trading_book(), 1.0)
    desired_notional = desired_frac * book * 0.5  # half-book at signal 1
    fill = world.broker.target_position(desired_notional, price, world.config.risk.round_trip_cost)
    # Recognize mark-to-market vs last recorded trading book
    eq = world.broker.equity()
    prev = world.store.get_kv("trader_last_eq", eq)
    delta = eq - float(prev)
    world.store.set_kv("trader_last_eq", eq)
    if abs(delta) >= 0.01:
        if delta > 0:
            world.ledger.post("assets.trading_book", "income.trading", delta, "mtm gain")
        else:
            world.ledger.post("income.trading", "assets.trading_book", -delta, "mtm loss")
    return [{"kind": "trade", "strategy": sid, "price": price, "fill": fill, "equity": eq}]


def publisher(world: World) -> list[dict[str, Any]]:
    # Package a product from recent deliveries every 8 ticks
    if world.tick % 8 != 0:
        return [{"kind": "publish", "skipped": True}]
    deliveries = list(world.config.paths().deliveries.glob("*.md"))
    if not deliveries:
        return [{"kind": "publish", "skipped": "no_deliveries"}]
    latest = max(deliveries, key=lambda p: p.stat().st_mtime)
    listing = world.config.paths().artifacts / "product_listing.md"
    copy = world.router.complete(
        f"Write a 1-page product listing based on this delivery:\n{latest.read_text()[:1500]}",
        tier="work",
    )
    listing.write_text(
        f"# Product listing — {world.config.firm_name}\n\n{copy}\n\nPay USDC to {world.identity.get('eth')}\n"
    )
    # Sim: a small sale appears
    if world.config.mode == "sim":
        pid = f"prod_{world.tick}"
        world.store.upsert_job(
            {
                "id": pid,
                "source": "product",
                "title": "Packaged automation kit",
                "status": "product_sold",
                "price_usd": 49.0,
                "payload": {},
            }
        )
        world.store.outcome("product", 49.0, True, "kit", "publisher", "digital_products")
        return [{"kind": "publish", "listing": str(listing), "sim_sale": 49.0}]
    return [{"kind": "publish", "listing": str(listing)}]


def scout(world: World) -> list[dict[str, Any]]:
    if world.tick % 10 != 0:
        return []
    ideas = [
        {
            "play": "productized",
            "offer": "48h operations automation",
            "price": 1200,
            "why": "repeats hunter's top tags",
        },
        {
            "play": "b2b_outbound",
            "offer": "Inbox SOP + weekly report bot",
            "price": 400,
            "why": "retainer geometry toward $5k",
        },
    ]
    note = world.router.complete(
        f"Pick the better next experiment given revenue={world.ledger.snapshot()['revenue_usd']} ideas={ideas}",
        tier="fast",
    )
    return [{"kind": "scout", "ideas": ideas, "note": note}]


def operator(world: World) -> list[dict[str, Any]]:
    # Plan VPS; buy only with quorum and cash. Sim: do not actually spend unless equity high.
    plan = {
        "provider": "crypto-friendly VPS (e.g. Hetzner via card once, or crypto VPS)",
        "spec": "2 vCPU / 4GB / 40GB",
        "est_usd": 6.0,
        "why": "jail for crafter + dashboard",
    }
    if world.treasury.operating_cash() < 100:
        world.human.ask(
            "vps",
            "Optional: API token for a VPS provider (Hetzner/DO) or a crypto VPS account. Engine will buy the smallest box when Treasurer+Director vote yes.",
            ["HETZNER_API_TOKEN"],
            "Compute. Can wait; local process is enough to earn labor.",
        )
        return [{"kind": "ops", "planned": plan, "bought": False}]
    votes = world.council.auto_votes_for_spend(
        6.0,
        world.treasury.operating_cash(),
        frozen="operator" in world.frozen,
        autonomy=world.reputation.autonomy_usd("operator", 80),
    )
    # Director proxy yes if labor is running
    votes["director"] = "yes" if world.ledger.snapshot()["revenue_usd"] > 0 else "no"
    ok, reason = world.council.quorum(f"vps_{world.tick}", "buy_infra", votes)
    bought = False
    if ok and world.config.mode == "sim" and not world.store.get_kv("vps_bought"):
        if world.treasury.pay(6.0, "expenses.infra", "sim vps month"):
            world.store.set_kv("vps_bought", True)
            bought = True
    return [{"kind": "ops", "planned": plan, "votes": votes, "bought": bought, "reason": reason}]


def auditor(world: World) -> list[dict[str, Any]]:
    notes = []
    # Sample last deliveries
    paid = world.store.jobs("paid")
    if paid:
        last = paid[-1]
        path = last.get("delivery_path")
        ok = bool(path and Path(path).exists() and Path(path).stat().st_size > 100)
        if ok:
            world.reputation.boost("crafter", 0.5, "audit pass")
            notes.append({"job": last["id"], "pass": True})
        else:
            world.reputation.slash("crafter", 4, "empty delivery")
            notes.append({"job": last["id"], "pass": False})
    # Trading sanity
    if world.broker.frozen:
        notes.append({"broker": "halted", "pass": True})
    # Ethics: keys never in events payload
    recent = world.store.events(10)
    for ev in recent:
        blob = json.dumps(ev).lower()
        if "mnemonic" in blob or "sol_secret" in blob:
            world.reputation.slash("operator", 50, "secret leakage")
            world.frozen.add("operator")
            notes.append({"ethics": "secret_leak", "pass": False})
    verdict = world.router.complete(
        f"Audit notes: {notes}. Slash anyone who sprayed generic proposals or traded uncertified.",
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
    patch = world.router.complete(
        f"Outcomes winrate={wins}/{n}. Write a playbook patch for closer and hunter.",
        tier="work",
        system=playbook(world, "improver"),
    )
    trial = world.config.paths().playbooks / "closer.trial.md"
    trial.write_text(patch)
    # Promote if winrate decent
    promoted = False
    if wins / n >= 0.4:
        (world.config.paths().playbooks / "closer.md").write_text(
            playbook(world, "closer") + "\n\n## Improver patch\n" + patch + "\n"
        )
        promoted = True
        world.store.outcome("playbook", 0, True, "promoted closer patch", "improver", "labor_studio")
    return [{"kind": "improve", "winrate": round(wins / n, 3), "promoted": promoted}]


def courier(world: World) -> list[dict[str, Any]]:
    # Keep Claude login request alive; add exchange when trading book wants live
    if world.treasury.trading_book() > 0 and world.config.mode == "live":
        world.human.ask(
            "exchange",
            "Create/KYC a crypto exchange or on-chain only. Provide API keys with trading+withdraw disabled until Risk says otherwise. Spot keys, IP allowlist.",
            ["EXCHANGE_API_KEY", "EXCHANGE_API_SECRET"],
            "Live execution. Paper runs without this.",
        )
    open_q = world.human.open()
    return [{"kind": "courier", "open_human_requests": len(open_q), "ids": [i["id"] for i in open_q]}]
