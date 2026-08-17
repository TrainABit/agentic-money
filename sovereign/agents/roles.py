from __future__ import annotations

import json
from datetime import timedelta
from pathlib import Path
from typing import Any

import numpy as np

from sovereign.capital.invoice import quote_usd
from sovereign.capital.payments import watch_and_collect
from sovereign.engine.heartbeat import fund_missions, playbook
from sovereign.engine.schedule import elapsed_days
from sovereign.engine.world import World
from sovereign.labor.boards import proposal_text, sim_client_accepts
from sovereign.labor.pipeline import accept_job, reject_job
from sovereign.markets.strategies import STRATEGIES
from sovereign.plays import PLAYS, attention_map, play_roi


def mechanic(world: World) -> list[dict[str, Any]]:
    """Self-heal. Runs first. Other agents keep working even if a login is missing."""
    if world.tools is None:
        from sovereign.tools.catalog import build_registry

        world.tools = build_registry()
        world.tools.bind(world)
    full = world.scheduler.claim(
        "mechanic_full",
        now=world.now,
        tick=world.tick,
        sim_every_ticks=10,
        live_every=timedelta(hours=world.config.live_timing.full_heal_cadence_hours),
    )
    report = world.use_tool("mechanic", "heal.repair", full=full)
    data = report.data if report.ok else {"error": report.error}

    if world.config.mode == "sim":
        from sovereign.heal.repair import thaw_cooled

        thawed = thaw_cooled(world, cooldown=5)
    else:
        thawed = []
        for agent in list(world.frozen):
            if not world.freeze_cooldown_elapsed(
                agent,
                sim_ticks=5,
                live_hours=world.config.live_timing.agent_freeze_cooldown_hours,
            ):
                continue
            world.reputation.boost(agent, 12, "mechanic cooldown")
            if not world.reputation.should_freeze(agent) and world.thaw(
                agent,
                "cooldown",
                automatic=True,
            ):
                thawed.append(agent)
            elif world.reputation.should_freeze(agent):
                world.freeze_since[agent] = world.tick
                world.freeze_info[agent]["since_tick"] = world.tick
                world.freeze_info[agent]["since_ts"] = world.stamp()
    cert_error = None
    if (
        world.config.mode == "sim"
        and world.tick > 0
        and not any(c.get("certified") for c in world.certified)
    ):
        cert = world.use_tool("mechanic", "market.certify")
        if not cert.ok:
            cert_error = cert.error
    return [
        {
            "kind": "mechanic",
            "health": data,
            "thawed": thawed,
            "certification_error": cert_error,
            "tools": world.tools.available_to("mechanic") if world.tools else [],
        }
    ]


def bookkeeper(world: World) -> list[dict[str, Any]]:
    r = world.use_tool("bookkeeper", "ledger.snapshot")
    snap = r.data if r.ok else world.ledger.snapshot(now=world.now)
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
    world.broker.roll_windows(world.now)
    if world.config.mode == "live":
        broker_thawed = world.broker.maybe_unfreeze(
            world.config.risk,
            world.tick,
            cooldown=timedelta(hours=world.config.live_timing.broker_cooldown_hours),
            now=world.now,
        )
    else:
        broker_thawed = world.broker.maybe_unfreeze(world.config.risk, world.tick, cooldown=5)
    if broker_thawed:
        trader_thawed = "trader" not in world.frozen
        freeze = world.freeze_info.get("trader") or {}
        if not trader_thawed and freeze.get("kind") == "circuit_breaker":
            trader_thawed = world.thaw("trader", "broker cooldown complete")
        if trader_thawed:
            out.append({"kind": "broker_thaw", "broker": world.broker.snapshot()})
        else:
            world.broker.frozen = True
            world.broker.halted_at = world.now
            world.broker.halt_reason = "trader_still_frozen"
            out.append({"kind": "broker_thaw_blocked", "reason": freeze.get("kind")})
    halt = world.broker.maybe_halt(
        world.config.risk,
        tick=world.tick,
        now=world.now if world.config.mode == "live" else None,
    )
    if halt:
        world.freeze("trader", halt, kind="circuit_breaker")
        out.append({"kind": "circuit_break", "reason": halt, "broker": world.broker.snapshot()})
    for agent, score in list(world.reputation.scores.items()):
        if score < 20 and agent not in world.frozen:
            fr = world.use_tool("risk", "governance.freeze", target=agent, reason=f"rep {score}")
            if fr.ok:
                out.append({"kind": "rep_freeze", "agent": agent, "score": score})
            else:
                out.append(
                    {
                        "kind": "rep_freeze_error",
                        "agent": agent,
                        "score": score,
                        "error": fr.error,
                    }
                )
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
            frozen = world.use_tool(
                "ethics",
                "governance.freeze",
                target="operator",
                reason="secret leakage",
            )
            notes.append("secret_leak" if frozen.ok else {"freeze_error": frozen.error})
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
    model_error = None
    if world.config.mode == "live" and world.scheduler.claim(
        "director_model",
        now=world.now,
        tick=world.tick,
        sim_every_ticks=20,
        live_every=timedelta(hours=world.config.live_timing.director_cadence_hours),
    ):
        model = world.use_tool(
            "director",
            "brain.complete",
            prompt=f"Weekly council. Trailing={snap['trailing_30d_usd']} gap_to_min={gap}. {note}. roi={roi}",
            tier="think",
            system=playbook(world, "director"),
        )
        if not model.ok:
            model_error = model.error
    world.store.set_kv("attention", attention_map(snap["trailing_30d_usd"], goals.minimum_usd, goals.recommended_usd, roi=roi))
    return [
        {
            "kind": "direct",
            "missions_opened": len(created),
            "gap_to_min": gap,
            "note": note,
            "roi": roi,
            "model_error": model_error,
        }
    ]


def hunter(world: World) -> list[dict[str, Any]]:
    live = world.config.mode == "live" and world.config.public_job_apis
    found_r = world.use_tool("hunter", "jobs.search", live=live)
    found = found_r.data if found_r.ok and isinstance(found_r.data, list) else None
    if found is None:
        found = world.board.search(tick=world.tick, live=live, include_sim=world.config.mode == "sim")
    taken = 0
    picked = []
    errors = []
    existing = {j["id"] for j in world.store.jobs()}
    for job in found:
        if job["id"] in existing:
            continue
        if job.get("fit", 0) < 0.45:
            continue
        if (
            world.config.mode == "live"
            and job.get("remote") is True
            and job.get("contact_verified") is not True
        ):
            job["status"] = "needs_channel"
        else:
            job["status"] = "open"
        if not job.get("price_usd"):
            job["price_usd"] = quote_usd(job)
        up = world.use_tool("hunter", "jobs.upsert", job=job)
        if not up.ok:
            errors.append({"id": job["id"], "operation": "jobs.upsert", "error": up.error})
            continue
        picked.append(job["title"])
        taken += 1
        if taken >= 4:
            break
    if live and taken == 0:
        world.use_tool(
            "hunter",
            "human.ask",
            service="job-platforms",
            instruction="Optional: Upwork/Fiverr tokens. Public boards already run. Or drop inbound leads as JSON in data/mail/inbox/.",
            fields=["note"],
            why="Extra channels. Not required to earn.",
        )
    return [{"kind": "hunt", "new": taken, "titles": picked, "errors": errors}]


def _needs_verified_channel(world: World, job: dict[str, Any]) -> bool:
    return (
        world.config.mode == "live"
        and job.get("remote") is True
        and job.get("contact_verified") is not True
    )


def closer(world: World) -> list[dict[str, Any]]:
    for j in world.store.jobs("applied"):
        if world.config.mode == "sim":
            expired = world.tick - int(j.get("applied_tick") or 0) >= 14
        else:
            age = elapsed_days(world.now, j.get("applied_ts"))
            expired = age is not None and age >= world.config.live_timing.proposal_expiry_days
        if expired:
            j["status"] = "expired"
            world.store.upsert_job(j)
    candidates = world.store.jobs("open") + world.store.jobs("queued_budget")
    results = []
    open_jobs = []
    for job in candidates:
        if _needs_verified_channel(world, job):
            job["status"] = "needs_channel"
            world.store.upsert_job(job)
            results.append(
                {
                    "id": job["id"],
                    "status": "needs_channel",
                    "url": job.get("url"),
                    "reason": "unverified_public_contact",
                }
            )
        else:
            open_jobs.append(job)
    if world.config.mode == "live":
        world.router.remaining_budget()
        if world.router.degraded:
            return [
                {
                    "kind": "close",
                    "skipped": "budget_degraded",
                    "queued": world.router.queued,
                    "results": results,
                }
            ]
    open_jobs.sort(key=lambda j: j.get("fit", 0), reverse=True)
    cap = world.config.apply_cap()
    day = world.now.date().isoformat()
    counts = dict(world.store.get_kv("apply_by_day") or {})
    already = int(counts.get(day, 0))
    send_limit = min(3 if world.config.mode == "sim" else 2, max(0, cap - already))
    rate = world.config.sim.close_rate if world.config.mode == "sim" else 1.0
    applied_today = already
    sent_count = 0
    for job in open_jobs:
        if sent_count >= send_limit:
            break
        job["price_usd"] = quote_usd(job)
        to = job.get("contact") or job.get("email")
        if not to and not (world.config.mode == "live" and job.get("remote") is True):
            from sovereign.labor.boards import extract_email

            to = extract_email(str(job.get("description") or ""))
        if not to:
            if world.config.mode == "live":
                job["status"] = "needs_channel"
                world.store.upsert_job(job)
                results.append({"id": job["id"], "status": "needs_channel", "url": job.get("url")})
                continue
            to = "client@unknown.local"
        pb = playbook(world, "closer", job["id"])
        blurb = world.use_tool(
            "closer",
            "brain.complete",
            prompt=f"Write a short proposal for: {job.get('title')}\n{job.get('description','')}\nPlaybook:\n{pb}",
            tier="work",
            system=pb,
        )
        if not blurb.ok:
            results.append(
                {
                    "id": job["id"],
                    "status": "error",
                    "operation": "brain.complete",
                    "error": blurb.error,
                }
            )
            continue
        blurb_text = str(blurb.data or "")
        if world.config.mode == "live" and not blurb_text:
            job["status"] = "queued_budget"
            world.store.upsert_job(job)
            results.append({"id": job["id"], "status": "queued_budget"})
            continue
        text = proposal_text(job, world.config.firm_name, blurb_text)
        job["proposal"] = text
        trial_p = world.config.paths().playbooks / "closer.trial.md"
        job["ab_variant"] = "trial" if trial_p.exists() and pb == trial_p.read_text() else "control"
        sent = world.use_tool(
            "closer",
            "mail.send",
            to=to,
            subject=f"Proposal: {job.get('title')} [{job['id']}]",
            body=text,
            job_id=job["id"],
            kind="proposal",
        )
        if not sent.ok:
            results.append(
                {
                    "id": job["id"],
                    "status": "error",
                    "operation": "mail.send",
                    "error": sent.error,
                }
            )
            continue
        job["applied_tick"] = world.tick
        job["applied_ts"] = world.stamp()
        sent_count += 1
        applied_today += 1
        if world.config.auto_accept():
            if sim_client_accepts(job, text, close_rate=rate):
                accept_job(world, job["id"], source="sim")
                results.append({"id": job["id"], "status": "accepted", "price": job.get("price_usd")})
            else:
                rejected = reject_job(world, job["id"], source="sim")
                results.append({"id": job["id"], "status": rejected["status"]})
        else:
            job["status"] = "applied"
            world.store.upsert_job(job)
            results.append({"id": job["id"], "status": "applied", "price": job.get("price_usd")})
    counts[day] = applied_today
    world.store.set_kv("apply_by_day", counts)
    return [{"kind": "close", "results": results}]


def _queued_craft_ready(world: World) -> bool:
    if world.config.mode != "live":
        return True
    remaining = world.router.remaining_budget()
    if not world.router.degraded:
        return True
    reason = str(world.router.last_error or "").lower()
    if "budget" in reason:
        return False
    if not world.scheduler.claim(
        "craft_provider_retry",
        now=world.now,
        tick=world.tick,
        sim_every_ticks=1,
        live_every=timedelta(hours=world.config.live_timing.craft_retry_hours),
    ):
        return False
    provider_ready = (
        world.config.models.provider == "claude_code"
        and world.router.claude.available()
        and remaining > 0
    )
    if not provider_ready:
        return False
    world.router.degraded = False
    world.router.last_error = None
    return True


def crafter(world: World) -> list[dict[str, Any]]:
    queued = world.store.jobs("queued_craft")
    if queued and not _queued_craft_ready(world):
        job = queued[0]
        return [
            {
                "kind": "craft",
                "job": job["id"],
                "status": "queued_craft",
                "skipped": "router_degraded",
                "reason": world.router.last_error,
            }
        ]
    queue = queued + world.store.jobs("accepted") + world.store.jobs("in_progress")
    if not queue:
        return [{"kind": "craft", "did": 0}]
    job = queue[0]
    produced = world.use_tool("crafter", "craft.produce", job=job)
    if not produced.ok:
        return [
            {
                "kind": "craft",
                "job": job["id"],
                "operation": "craft.produce",
                "error": produced.error,
            }
        ]
    artifact = produced.data
    if isinstance(artifact, dict) and artifact.get("queued"):
        if world.config.mode == "live":
            world.scheduler.mark("craft_provider_retry", now=world.now)
        job["status"] = "queued_craft"
        job.setdefault("queued_craft_ts", world.stamp())
        job["queued_craft_reason"] = world.router.last_error or "model unavailable"
        world.store.upsert_job(job)
        return [
            {
                "kind": "craft",
                "job": job["id"],
                "status": "queued_craft",
                "queued": True,
                "reason": job["queued_craft_reason"],
            }
        ]
    if not isinstance(artifact, dict) or not artifact.get("delivery"):
        return [{"kind": "craft", "job": job["id"], "error": "no artifact"}]
    job["status"] = "delivered"
    job.pop("queued_craft_ts", None)
    job.pop("queued_craft_reason", None)
    job["delivery_path"] = artifact["delivery"]
    job["entry"] = artifact.get("entry")
    job["files"] = artifact.get("files")
    world.store.upsert_job(job)
    world.store.outcome("delivery", float(job.get("price_usd") or 0), True, job["title"], "crafter", "labor_studio")
    world.reputation.boost("crafter", 2.0, "delivered")
    from sovereign.memory.skills import record

    record(world, "crafter.deliver", True, float(job.get("price_usd") or 0))
    dest = job.get("contact") or job.get("email")
    mail_error = None
    if dest or world.config.mode == "sim":
        sent = world.use_tool(
            "crafter",
            "mail.send",
            to=dest or "client@unknown.local",
            subject=f"Delivery: {job.get('title')} [{job['id']}]",
            body=f"Files are ready. Entry: {artifact.get('entry')}\nInvoice follows.",
            job_id=job["id"],
            kind="delivery",
        )
        if not sent.ok:
            mail_error = sent.error
    return [
        {
            "kind": "craft",
            "job": job["id"],
            "path": artifact["delivery"],
            "files": artifact.get("files"),
            "mail_error": mail_error,
        }
    ]


def treasurer(world: World) -> list[dict[str, Any]]:
    issued = []
    errors = []
    for job in world.store.jobs("delivered"):
        income = "income.products" if job.get("source") == "product" else "income.labor"
        if "retainer" in str(job.get("title", "")).lower():
            income = "income.retainers"
        inv_r = world.use_tool("treasurer", "invoice.issue", job=job, income_account=income)
        if not inv_r.ok:
            errors.append({"job": job["id"], "operation": "invoice.issue", "error": inv_r.error})
            continue
        inv = inv_r.data
        if not isinstance(inv, dict):
            errors.append({"job": job["id"], "operation": "invoice.issue", "error": "invalid result"})
            continue
        issued.append(inv["id"])
        dest = job.get("contact") or job.get("email")
        if dest or world.config.mode == "sim":
            sent = world.use_tool(
                "treasurer",
                "mail.send",
                to=dest or "client@unknown.local",
                subject=f"Invoice {inv['id']} — ${inv['amount']:.0f} USDC [{job['id']}]",
                body=(
                    f"Pay ${inv['amount']:.2f} USDC to ETH `{inv['eth_address']}` "
                    f"or SOL `{inv.get('sol_address')}` memo {inv['memo']}"
                ),
                job_id=job["id"],
                kind="invoice",
            )
            if not sent.ok:
                errors.append({"invoice": inv["id"], "operation": "mail.send", "error": sent.error})

    collected = []
    if world.config.autocollect():
        delay = world.config.sim.pay_delay_ticks
        for inv in world.store.invoices("open"):
            issued_tick = int(inv.get("issued_tick") or 0)
            if world.tick - issued_tick >= delay:
                got = world.use_tool("treasurer", "invoice.collect", ref=inv["id"], source="autocollect")
                if got.ok and isinstance(got.data, dict):
                    collected.append(got.data)
                elif not got.ok:
                    errors.append({"invoice": inv["id"], "operation": "invoice.collect", "error": got.error})
    else:
        collected.extend(watch_and_collect(world))
        from sovereign.capital.invoice import void

        for inv in world.store.invoices("open"):
            if world.config.mode == "sim":
                aged = world.tick - int(inv.get("issued_tick") or 0) >= 90
            else:
                age = elapsed_days(world.now, inv.get("ts"))
                aged = age is not None and age >= world.config.live_timing.invoice_void_days
            if aged:
                void(world, inv["id"], reason="aged")

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
            "errors": errors,
            "policy": world.treasury.policy_status(),
        }
    ]


def _oos_sharpe(report: dict[str, Any]) -> float:
    try:
        return float((report.get("oos") or {}).get("sharpe", float("-inf")))
    except (TypeError, ValueError):
        return float("-inf")


def _strategy_warmup(strategy: Any) -> int:
    values = [
        getattr(strategy, name, 0)
        for name in ("lookback", "vol_lookback", "fast", "slow")
    ]
    return max((int(v) for v in values if isinstance(v, (int, float))), default=0)


def trader(world: World) -> list[dict[str, Any]]:
    certified = [c for c in world.certified if c.get("certified")]
    if not certified:
        return [{"kind": "trade", "skipped": "none_certified"}]
    if world.broker.frozen or "trader" in world.frozen:
        return [{"kind": "trade", "skipped": "frozen"}]
    if world.treasury.trading_book() <= 0 and world.broker.cash <= 0:
        return [{"kind": "trade", "skipped": "no_book"}]

    selected = max(certified, key=_oos_sharpe)
    sid = str(selected.get("strategy_id") or "")
    strat = STRATEGIES.get(sid)
    if strat is None:
        return [{"kind": "trade", "skipped": "unknown_strategy", "strategy": sid}]
    close = np.array(world.market_close, dtype=float)
    warmup = _strategy_warmup(strat)
    if len(close) <= warmup:
        return [
            {
                "kind": "trade",
                "skipped": "warmup",
                "strategy": sid,
                "required": warmup + 1,
                "available": len(close),
            }
        ]
    if world.config.mode == "live":
        idx = len(close) - 1
    else:
        idx = min(len(close) - 1, max(warmup + 5, 60 + world.tick))
    price = float(close[idx])
    world.broker.mark(price)
    world.broker.roll_windows(world.now)
    pos = strat.positions(close[: idx + 1])
    desired_frac = float(pos[-2] if len(pos) > 1 else 0.0)
    book = max(world.broker.equity(), world.treasury.trading_book(), 1.0)
    leverage_cap = book * max(0.0, world.config.risk.max_leverage)
    wallet_cap = max(0.0, world.config.risk.hot_wallet_cap_usd)
    signal_cap = book * max(0.0, world.config.risk.trading_risk_per_signal)
    max_notional = min(leverage_cap, wallet_cap, signal_cap)
    signal_strength = max(-1.0, min(1.0, desired_frac))
    desired_notional = signal_strength * max_notional
    fill = world.broker.target_position(desired_notional, price, world.config.risk.round_trip_cost)
    eq = world.broker.equity()
    prev = world.store.get_kv("trader_last_eq", eq)
    delta = eq - float(prev)
    world.store.set_kv("trader_last_eq", eq)
    if abs(delta) >= 0.01:
        if delta > 0:
            world.ledger.post("assets.trading_book", "income.trading_paper", delta, "mtm gain", ts=world.stamp())
        else:
            world.ledger.post("income.trading_paper", "assets.trading_book", -delta, "mtm loss", ts=world.stamp())
    return [
        {
            "kind": "trade",
            "strategy": sid,
            "price": price,
            "fill": fill,
            "equity": eq,
            "position_cap_usd": max_notional,
        }
    ]


def publisher(world: World) -> list[dict[str, Any]]:
    if not world.scheduler.claim(
        "publisher_model",
        now=world.now,
        tick=world.tick,
        sim_every_ticks=8,
        live_every=timedelta(hours=world.config.live_timing.publisher_cadence_hours),
    ):
        return [{"kind": "publish", "skipped": True}]
    deliveries = [p for p in world.config.paths().deliveries.rglob("*") if p.is_file()]
    if not deliveries:
        return [{"kind": "publish", "skipped": "no_deliveries"}]
    latest = max(deliveries, key=lambda p: p.stat().st_mtime)
    listing = world.config.paths().artifacts / "product_listing.md"
    generated = world.use_tool(
        "publisher",
        "brain.complete",
        prompt=f"Write a 1-page product listing based on this delivery:\n{latest.read_text(errors='ignore')[:1500]}",
        tier="work",
    )
    if not generated.ok:
        return [
            {
                "kind": "publish",
                "operation": "brain.complete",
                "error": generated.error,
            }
        ]
    copy = str(generated.data or "")
    if not copy:
        return [{"kind": "publish", "skipped": "empty_model_result"}]
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
    if not world.scheduler.claim(
        "scout_model",
        now=world.now,
        tick=world.tick,
        sim_every_ticks=10,
        live_every=timedelta(hours=world.config.live_timing.scout_cadence_hours),
    ):
        return [{"kind": "scout", "offers": len(catalog)}]
    model = world.use_tool(
        "scout",
        "brain.complete",
        prompt=f"Pick the better next experiment given trailing={world.ledger.snapshot(now=world.now)['trailing_30d_usd']} offers={catalog}",
        tier="fast",
    )
    if not model.ok:
        return [
            {
                "kind": "scout",
                "ideas": catalog,
                "operation": "brain.complete",
                "error": model.error,
            }
        ]
    return [{"kind": "scout", "ideas": catalog, "note": str(model.data or "")}]


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
    if not world.scheduler.claim(
        "auditor_model",
        now=world.now,
        tick=world.tick,
        sim_every_ticks=1,
        live_every=timedelta(hours=world.config.live_timing.auditor_cadence_hours),
    ):
        return [{"kind": "audit", "skipped": True}]
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
    model = world.use_tool(
        "auditor",
        "brain.complete",
        prompt=f"Audit notes: {notes}. Slash spray or uncertified live trades.",
        tier="fast",
        system="Return a one-line verdict.",
    )
    if not model.ok:
        return [
            {
                "kind": "audit",
                "notes": notes,
                "operation": "brain.complete",
                "error": model.error,
            }
        ]
    return [{"kind": "audit", "notes": notes, "verdict": str(model.data or "")}]


def improver(world: World) -> list[dict[str, Any]]:
    if not world.scheduler.claim(
        "improver_model",
        now=world.now,
        tick=world.tick,
        sim_every_ticks=7,
        live_every=timedelta(hours=world.config.live_timing.improver_cadence_hours),
    ):
        return []
    from sovereign.memory.playbooks import promote_trial, revert_trial
    from sovereign.memory.skills import record

    outcomes = world.store.outcomes(40)
    wins = sum(1 for o in outcomes if o["success"])
    n = len(outcomes) or 1
    roi = play_roi(outcomes)
    record(world, "improver.cycle", True, 0)
    ab = dict(world.store.get_kv("ab_closer") or {})
    trial_p = world.config.paths().playbooks / "closer.trial.md"
    promoted = False
    reverted = False
    model_error = None
    tn, cn = int(ab.get("trial_n", 0)), int(ab.get("control_n", 0))
    tw, cw = float(ab.get("trial_usd", 0)), float(ab.get("control_usd", 0))
    if trial_p.exists() and tn >= 6 and cn >= 6:
        t_avg = tw / max(tn, 1)
        c_avg = cw / max(cn, 1)
        if t_avg > c_avg * 1.05:
            promoted = promote_trial(world.config.paths().playbooks, "closer")
            world.store.outcome("playbook", 0, True, "promoted closer trial", "improver", "labor_studio")
        else:
            reverted = revert_trial(world.config.paths().playbooks, "closer")
            world.store.outcome("playbook", 0, False, "reverted closer trial", "improver", "labor_studio")
        world.store.set_kv("ab_closer", {"control_n": 0, "trial_n": 0, "control_usd": 0.0, "trial_usd": 0.0})
    elif not trial_p.exists():
        patch = world.use_tool(
            "improver",
            "brain.complete",
            prompt=f"Outcomes winrate={wins}/{n} roi={roi} ab={ab}. Write a closer playbook trial.",
            tier="work",
            system=playbook(world, "improver"),
        )
        if patch.ok and patch.data:
            wrote = world.use_tool(
                "improver",
                "playbook.write_trial",
                agent="closer",
                body=str(patch.data),
            )
            if not wrote.ok:
                model_error = wrote.error
        else:
            model_error = patch.error or "empty model result"
    override = world.store.get_kv("attention_override") or {}
    if world.config.mode == "sim":
        age_days = world.tick * (world.config.tick_hours / 24.0)
    else:
        age_days = elapsed_days(world.now, world.identity.get("born")) or 0.0
    for p in PLAYS:
        play_usd = sum(float(o.get("usd") or 0) for o in outcomes if o.get("play_id") == p.id and o.get("success"))
        if age_days >= p.kill_after_days_if_zero and play_usd <= 0:
            override[p.id] = 0.0
        elif roi.get(p.id, 0) <= 0 and age_days > 14:
            override[p.id] = max(0.01, attention_map(0, 2000, 5000).get(p.id, 0.05) * 0.5)
    if override:
        world.store.set_kv("attention_override", override)
    return [
        {
            "kind": "improve",
            "winrate": round(wins / n, 3),
            "promoted": promoted,
            "reverted": reverted,
            "roi": roi,
            "ab": ab,
            "model_error": model_error,
        }
    ]


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
