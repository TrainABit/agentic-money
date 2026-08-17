from __future__ import annotations

import json
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from typing import Any

from sovereign.capital.ledger import Ledger
from sovereign.capital.treasury import Treasury
from sovereign.capital.wallet import Wallet, derive_solana_keypair
from sovereign.channels.human import HumanInbox
from sovereign.config import EngineConfig
from sovereign.engine.schedule import Clock, Scheduler, SystemClock, aware_utc, elapsed, parse_datetime
from sovereign.governance.council import Council
from sovereign.governance.reputation import Reputation
from sovereign.labor.boards import JobBoard
from sovereign.markets.data import certify, fetch_closes, synthetic_ohlc
from sovereign.markets.paper import PaperBroker
from sovereign.memory.playbooks import seed_playbooks
from sovereign.memory.store import Store, iso
from sovereign.runtime.router import Router


@dataclass
class World:
    config: EngineConfig
    store: Store
    ledger: Ledger
    treasury: Treasury
    wallet: Wallet
    router: Router
    board: JobBoard
    council: Council
    reputation: Reputation
    human: HumanInbox
    broker: PaperBroker
    tick: int = 0
    now: datetime = field(default_factory=lambda: SystemClock().now())
    frozen: set[str] = field(default_factory=set)
    certified: list[dict[str, Any]] = field(default_factory=list)
    identity: dict[str, str] = field(default_factory=dict)
    market_close: list[float] = field(default_factory=list)
    last_prices: dict[str, Any] = field(default_factory=dict)
    freeze_since: dict[str, int] = field(default_factory=dict)
    freeze_info: dict[str, dict[str, Any]] = field(default_factory=dict)
    tools: Any = None
    comms: Any = None
    clock: Clock = field(default_factory=SystemClock)
    scheduler: Scheduler = field(init=False)

    def __post_init__(self) -> None:
        self.now = aware_utc(self.now)
        self.scheduler = Scheduler(self.store, self.config.mode)

    def stamp(self) -> str:
        return iso(aware_utc(self.now))

    def start_tick(self) -> None:
        marker = self.store.get_kv("tick_start") or {}
        try:
            marked_tick = int(marker.get("tick", 0))
        except (TypeError, ValueError):
            marked_tick = 0
        self.tick = max(self.tick, marked_tick) + 1
        if self.config.mode == "sim":
            self.now = aware_utc(self.now) + timedelta(hours=self.config.tick_hours)
        else:
            self.now = aware_utc(self.clock.now())
        self.store.set_kv(
            "tick_start",
            {"tick": self.tick, "started_ts": self.stamp(), "status": "started"},
        )

    def finish_tick(self) -> None:
        marker = dict(self.store.get_kv("tick_start") or {})
        try:
            marked_tick = int(marker.get("tick") or -1)
        except (TypeError, ValueError):
            marked_tick = -1
        if marked_tick != self.tick:
            marker = {"tick": self.tick, "started_ts": self.stamp()}
        marker["status"] = "completed"
        marker["completed_ts"] = (
            iso(aware_utc(self.clock.now()))
            if self.config.mode == "live"
            else self.stamp()
        )
        self.store.set_kv("tick_start", marker)

    def resume_tick_marker(self) -> None:
        marker = self.store.get_kv("tick_start") or {}
        try:
            marked_tick = int(marker.get("tick", 0))
        except (TypeError, ValueError):
            return
        if marked_tick <= self.tick:
            return
        self.tick = marked_tick
        marked_now = parse_datetime(marker.get("started_ts"))
        if marked_now is not None:
            self.now = marked_now

    def freeze(self, agent: str, reason: str, *, kind: str | None = None) -> None:
        explicit_kind = kind is not None
        kind = kind or self._infer_freeze_kind(reason)
        auto_thaw = kind not in {"ethics", "manual", "circuit_breaker"}
        if self.config.mode == "sim" and kind == "manual" and not explicit_kind:
            auto_thaw = True
        first = agent not in self.frozen
        self.frozen.add(agent)
        if first:
            self.freeze_since[agent] = self.tick
        current = self.freeze_info.get(agent) or {}
        if first or (bool(current.get("auto_thaw", True)) and not auto_thaw):
            self.freeze_info[agent] = {
                "since_tick": self.freeze_since.get(agent, self.tick),
                "since_ts": self.stamp(),
                "reason": reason,
                "kind": kind,
                "auto_thaw": auto_thaw,
            }
        if first:
            self.store.emit(
                "freeze",
                {"agent": agent, "reason": reason, "freeze_kind": kind},
                "risk",
            )

    def thaw(self, agent: str, reason: str, *, automatic: bool = False) -> bool:
        info = self.freeze_info.get(agent) or {}
        is_automatic = automatic or reason == "cooldown"
        if is_automatic and not bool(info.get("auto_thaw", True)):
            return False
        if agent not in self.frozen:
            return False
        self.frozen.discard(agent)
        self.freeze_since.pop(agent, None)
        self.freeze_info.pop(agent, None)
        self.store.emit(
            "thaw",
            {
                "agent": agent,
                "reason": reason,
                "freeze_kind": info.get("kind"),
            },
            "mechanic",
        )
        return True

    def freeze_cooldown_elapsed(self, agent: str, *, sim_ticks: int, live_hours: float) -> bool:
        info = self.freeze_info.get(agent) or {}
        if not bool(info.get("auto_thaw", True)):
            return False
        if self.config.mode == "sim":
            since = int(self.freeze_since.get(agent, self.tick))
            return self.tick - since >= sim_ticks
        age = elapsed(self.now, info.get("since_ts"))
        return age is not None and age >= timedelta(hours=max(0.0, live_hours))

    @staticmethod
    def _infer_freeze_kind(reason: str) -> str:
        lowered = reason.lower()
        if "secret leak" in lowered or lowered.startswith("ethics"):
            return "ethics"
        if "daily_halt" in lowered or "weekly_halt" in lowered or "circuit" in lowered:
            return "circuit_breaker"
        if lowered.startswith("rep "):
            return "risk"
        if lowered.startswith("exception:"):
            return "runtime"
        return "manual"

    def use_tool(self, caller: str, name: str, /, **kwargs: Any) -> Any:
        if self.tools is None:
            raise RuntimeError("tools unbound")
        return self.tools.call(caller, name, **kwargs)

    def _load_certified(self, meta: dict[str, Any]) -> None:
        meta_reports = meta.get("certified")
        dedicated_reports = self.store.get_kv("certified")
        meta_ts = parse_datetime(meta.get("certified_ts"))
        dedicated_ts = parse_datetime(self.store.get_kv("certified_ts"))
        use_dedicated = isinstance(dedicated_reports, list) and (
            "certified" not in meta
            or meta_ts is None
            or (dedicated_ts is not None and dedicated_ts >= meta_ts)
        )
        if use_dedicated:
            self.certified = list(dedicated_reports)
        elif isinstance(meta_reports, list):
            self.certified = list(meta_reports)

    def persist_kv(self) -> None:
        self.store.set_kv(
            "meta",
            {
                "tick": self.tick,
                "now": self.stamp(),
                "frozen": sorted(self.frozen),
                "certified": self.certified,
                "certified_ts": self.store.get_kv("certified_ts"),
                "identity": self.identity,
                "reputation": self.reputation.scores,
                "broker": self.broker.snapshot(),
                "last_prices": self.last_prices,
                "provider": self.router.snapshot(),
                "freeze_since": self.freeze_since,
                "freeze_info": self.freeze_info,
            },
        )

    def load_kv(self) -> None:
        self.ledger._bal_cache = None
        meta = self.store.get_kv("meta") or {}
        self.tick = int(meta.get("tick", 0))
        if meta.get("now"):
            loaded_now = parse_datetime(meta["now"])
            if loaded_now is not None:
                self.now = loaded_now
        self.frozen = set(meta.get("frozen") or [])
        self._load_certified(meta)
        self.identity = dict(meta.get("identity") or {})
        if meta.get("reputation"):
            self.reputation.scores = {k: float(v) for k, v in meta["reputation"].items()}
        b = meta.get("broker") or {}
        if b:
            self.broker.cash = float(b.get("cash", self.broker.cash))
            self.broker.position = float(b.get("position", 0))
            self.broker.last_price = float(b.get("last_price", 0))
            self.broker.frozen = bool(b.get("frozen", False))
            self.broker.day_start_equity = float(b.get("day_start_equity") or 0)
            self.broker.week_start_equity = float(b.get("week_start_equity") or 0)
            self.broker.day_key = str(b.get("day_key") or "")
            self.broker.week_key = str(b.get("week_key") or "")
            ht = b.get("halt_tick")
            self.broker.halt_tick = int(ht) if ht is not None else None
            self.broker.halted_at = parse_datetime(b.get("halted_at"))
            self.broker.halt_reason = str(b.get("halt_reason") or "") or None
            if self.broker.frozen and self.broker.halted_at is None:
                self.broker.halted_at = self.now
        self.last_prices = dict(meta.get("last_prices") or {})
        self.freeze_since = {k: int(v) for k, v in (meta.get("freeze_since") or {}).items()}
        self.freeze_info = {
            str(k): dict(v)
            for k, v in (meta.get("freeze_info") or {}).items()
            if isinstance(v, dict)
        }
        for agent in self.frozen:
            if agent not in self.freeze_info:
                auto_thaw = self.config.mode == "sim"
                self.freeze_info[agent] = {
                    "since_tick": self.freeze_since.get(agent, self.tick),
                    "since_ts": self.stamp(),
                    "reason": "legacy freeze",
                    "kind": "legacy" if auto_thaw else "manual",
                    "auto_thaw": auto_thaw,
                }
        if meta.get("provider"):
            self.router.restore(meta["provider"])
        self.resume_tick_marker()

    def status(self) -> dict[str, Any]:
        snap = self.ledger.snapshot(now=self.now)
        goals = self.config.goals
        rev = snap["trailing_30d_usd"]
        counts = self.store.job_counts()
        return {
            "firm": self.config.firm_name,
            "mode": self.config.mode,
            "tick": self.tick,
            "ts": self.stamp(),
            "identity": self.identity,
            "wallet": self.wallet.public(),
            "credentials_present": self.wallet.credential_flags(),
            "ledger": snap,
            "treasury": self.treasury.policy_status(),
            "pipeline": counts,
            "invoices_open": len(self.store.invoices("open")),
            "invoices_paid": len(self.store.invoices("paid")),
            "comms": self.comms.counts() if self.comms is not None else None,
            "offers": self.store.offers(),
            "mail_out": len(self.store.mail(direction="out")),
            "mail_in": len(self.store.mail(direction="in")),
            "goals": {
                "minimum": goals.minimum_usd,
                "recommended": goals.recommended_usd,
                "good": goals.good_usd,
                "run_rate_usd": rev,
                "lifetime_usd": snap["revenue_usd"],
                "progress_min": round(min(1.0, rev / goals.minimum_usd), 4),
                "progress_rec": round(min(1.0, rev / goals.recommended_usd), 4),
                "progress_good": round(min(1.0, rev / goals.good_usd), 4),
            },
            "certified_strategies": [c for c in self.certified if c.get("certified")],
            "rejected_strategies": [c for c in self.certified if not c.get("certified")],
            "broker": self.broker.snapshot(),
            "frozen_agents": sorted(self.frozen),
            "freeze_details": self.freeze_info,
            "reputation": self.reputation.scores,
            "open_jobs": counts.get("open", 0),
            "active_jobs": counts.get("accepted", 0) + counts.get("in_progress", 0),
            "human_inbox": self.human.open(),
            "cognition": self.router.snapshot(),
            "recent_events": self.store.events(25),
            "missions": self.store.missions(),
            "health": self.store.get_kv("health"),
            "skills": self.store.get_kv("skills"),
            "tools": None if self.tools is None else {
                "names": self.tools.names(),
                "by_agent": {a: self.tools.available_to(a) for a in (
                    "hunter", "closer", "crafter", "trader", "treasurer",
                    "mechanic", "improver", "courier", "director", "risk",
                    "bookkeeper", "ethics", "auditor", "operator", "publisher", "scout",
                )},
            },
        }


def bootstrap(config: EngineConfig, *, heal: bool = True, clock: Clock | None = None) -> World:
    paths = config.paths()
    paths.ensure()
    seed_playbooks(paths.playbooks)
    store = Store(paths.db)
    active_clock = clock or SystemClock()
    initial_now = aware_utc(active_clock.now())
    ledger = Ledger(store)
    treasury = Treasury(ledger, config)
    wallet = Wallet(paths.secrets, paths.master_key)
    bundle_pub = wallet.load_or_create()
    derived_sol_address, _ = derive_solana_keypair(bundle_pub.mnemonic)
    if (
        bundle_pub.sol_address != derived_sol_address
        and not store.get_kv("legacy_sol_wallet_notified")
    ):
        store.emit(
            "legacy_sol_wallet",
            {
                "address": bundle_pub.sol_address,
                "warning": (
                    "This pre-migration Solana key is recoverable from secrets.enc, "
                    "not from the mnemonic alone."
                ),
            },
            "mechanic",
        )
        store.set_kv("legacy_sol_wallet_notified", True)
    router = Router(config)
    world = World(
        config=config,
        store=store,
        ledger=ledger,
        treasury=treasury,
        wallet=wallet,
        router=router,
        board=JobBoard(sim=config.mode == "sim"),
        council=Council(store),
        reputation=Reputation(store.get_kv("meta", {}).get("reputation") if store.get_kv("meta") else {}),
        human=HumanInbox(paths),
        broker=PaperBroker(cash=0.0),
        clock=active_clock,
        now=initial_now,
        identity={
            "name": config.firm_name,
            "eth": bundle_pub.eth_address,
            "sol": bundle_pub.sol_address,
            "mandate": config.mandate,
            "born": iso(initial_now),
        },
    )
    from sovereign.agents.spec import roster
    from sovereign.comms.bus import Bus
    from sovereign.tools.catalog import build_registry

    world.tools = build_registry()
    world.tools.bind(world)
    world.comms = Bus(store, roster())
    if store.get_kv("meta"):
        world.load_kv()
        world.identity.setdefault("name", config.firm_name)
        world.identity["eth"] = bundle_pub.eth_address
        world.identity["sol"] = bundle_pub.sol_address
    else:
        store.emit(
            "genesis",
            {
                "firm": config.firm_name,
                "eth": bundle_pub.eth_address,
                "sol": bundle_pub.sol_address,
                "mode": config.mode,
            },
            agent="director",
        )
        if config.mode == "sim":
            ledger.post("assets.usdc", "equity.treasury", 250.0, "sim genesis float", ts=world.stamp())
        world.human.ask(
            service="claude",
            instruction="Install Claude Code if needed, then run `claude login` on the host using the Claude Pro/Max subscription (not an API key). Reply with {\"ok\": \"1\"}.",
            fields=["ok"],
            why="Live cognition uses the subscription CLI. Sim brain already runs.",
        )
        world.human.ask(
            service="bank_kyc",
            instruction="When you can, open Stripe or a business account (Mercury/Wise). Drop field notes here. Until then USDC is the treasury. A KYC packet is in artifacts/kyc_packet.md.",
            fields=["STRIPE_SECRET", "note"],
            why="Fiat off-ramp. Not required to earn in USDC.",
        )
        packet = paths.artifacts / "kyc_packet.md"
        packet.write_text(
            f"# KYC packet — {config.firm_name}\n\n"
            f"- Activity: remote software, research, automation services; systematic crypto on a walled book.\n"
            f"- ETH: {bundle_pub.eth_address}\n"
            f"- SOL: {bundle_pub.sol_address}\n"
            f"- Expected volume: ${config.goals.minimum_usd:.0f}–${config.goals.good_usd:.0f}/mo.\n"
            f"- Operators: autonomous agents under the human legal person.\n"
        )
        world._load_certified({})
        world.resume_tick_marker()
    from sovereign.heal.repair import setup as heal_setup

    if heal:
        heal_setup(world, full=False)
        world.persist_kv()
    return world


def load_prices(world: World, force: bool = False) -> None:
    if world.config.mode == "live":
        if not force and not world.scheduler.claim(
            "market_prices",
            now=world.now,
            tick=world.tick,
            sim_every_ticks=1,
            live_every=timedelta(hours=world.config.live_timing.price_refresh_hours),
        ):
            return
        if force:
            world.scheduler.mark("market_prices", now=world.now)
    else:
        fetched_at = int(world.last_prices.get("tick") or -10**9)
        if world.market_close and not force and world.tick - fetched_at < world.config.price_refresh_every():
            return
    closes = None
    source = "synthetic"
    if world.config.mode == "live" and world.config.fetch_market_data:
        try:
            closes, source = fetch_closes()
            if len(closes) == 0:
                raise ValueError("market source returned no closes")
            world.store.emit("market_fetch", {"source": source, "n": int(len(closes))}, "trader")
        except Exception as e:
            world.scheduler.retry_after(
                "market_prices",
                now=world.now,
                delay=timedelta(
                    minutes=world.config.live_timing.price_failure_retry_minutes
                ),
            )
            world.store.emit("market_fetch_failed", {"error": str(e)}, "trader")
            if world.market_close:
                return
            world.last_prices["source"] = "none"
            world.last_prices["tick"] = world.tick
            world.last_prices["ts"] = world.stamp()
            return
    if closes is None:
        if world.config.mode == "live":
            world.last_prices["source"] = "none"
            world.last_prices["tick"] = world.tick
            world.last_prices["ts"] = world.stamp()
            return
        closes = synthetic_ohlc()
        source = "synthetic"
    world.market_close = [float(x) for x in closes]
    world.last_prices["BTCUSDT"] = float(closes[-1])
    world.last_prices["source"] = source
    world.last_prices["tick"] = world.tick
    world.last_prices["ts"] = world.stamp()


def ensure_certified(world: World, force: bool = False) -> None:
    if world.config.mode == "live":
        cadence_hours = (
            world.config.live_timing.recertify_hours
            if world.certified
            else world.config.live_timing.certification_retry_hours
        )
        if not force and not world.scheduler.claim(
            "market_certification",
            now=world.now,
            tick=world.tick,
            sim_every_ticks=1,
            live_every=timedelta(hours=cadence_hours),
        ):
            return
        if force:
            world.scheduler.mark("market_certification", now=world.now)
    else:
        last = int(world.store.get_kv("certified_tick") or 0)
        if world.certified and not force and world.tick - last < world.config.recertify_every():
            return
    load_prices(world)
    source = str(world.last_prices.get("source") or "")
    if world.config.mode == "live" and (not world.market_close or source in {"none", "synthetic"} or source.startswith("synthetic")):
        world.scheduler.retry_after(
            "market_certification",
            now=world.now,
            delay=timedelta(
                minutes=world.config.live_timing.certification_failure_retry_minutes
            ),
        )
        world.store.emit("cert_skipped", {"reason": "no_live_prices", "source": source}, "risk")
        return
    if not world.market_close:
        load_prices(world, force=True)
        if not world.market_close:
            if world.config.mode == "live":
                world.scheduler.retry_after(
                    "market_certification",
                    now=world.now,
                    delay=timedelta(
                        minutes=world.config.live_timing.certification_failure_retry_minutes
                    ),
                )
            return
    import numpy as np

    try:
        reports = certify(np.array(world.market_close, dtype=float), world.config.risk)
    except Exception as e:
        if world.config.mode != "live":
            raise
        world.scheduler.retry_after(
            "market_certification",
            now=world.now,
            delay=timedelta(
                minutes=world.config.live_timing.certification_failure_retry_minutes
            ),
        )
        world.store.emit("cert_failed", {"error": str(e)}, "risk")
        return
    world.certified = reports
    world.store.set_kv("certified", reports)
    world.store.set_kv("certified_tick", world.tick)
    world.store.set_kv("certified_ts", world.stamp())
    artifact = world.config.paths().artifacts / "strategy_certification.json"
    artifact.write_text(json.dumps(reports, indent=2))
    for r in reports:
        world.store.emit("strategy_cert", r, "risk")
