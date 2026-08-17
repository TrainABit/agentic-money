from __future__ import annotations

import json
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any

from sovereign.capital.ledger import Ledger
from sovereign.capital.treasury import Treasury
from sovereign.capital.wallet import Wallet
from sovereign.channels.human import HumanInbox
from sovereign.config import EngineConfig
from sovereign.governance.council import Council
from sovereign.governance.reputation import Reputation
from sovereign.labor.boards import JobBoard
from sovereign.markets.data import certify, fetch_closes, synthetic_ohlc
from sovereign.markets.paper import PaperBroker
from sovereign.memory.playbooks import seed_playbooks
from sovereign.memory.store import Store, iso, utcnow
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
    now: datetime = field(default_factory=utcnow)
    frozen: set[str] = field(default_factory=set)
    certified: list[dict[str, Any]] = field(default_factory=list)
    identity: dict[str, str] = field(default_factory=dict)
    market_close: list[float] = field(default_factory=list)
    last_prices: dict[str, float] = field(default_factory=dict)
    freeze_since: dict[str, int] = field(default_factory=dict)
    tools: Any = None

    def stamp(self) -> str:
        if self.config.mode == "live":
            return iso()
        return iso(self.now)

    def freeze(self, agent: str, reason: str) -> None:
        first = agent not in self.frozen
        self.frozen.add(agent)
        if first:
            self.freeze_since[agent] = self.tick
            self.store.emit("freeze", {"agent": agent, "reason": reason}, "risk")

    def thaw(self, agent: str, reason: str) -> None:
        self.frozen.discard(agent)
        self.freeze_since.pop(agent, None)
        self.store.emit("thaw", {"agent": agent, "reason": reason}, "mechanic")

    def use_tool(self, caller: str, name: str, **kwargs: Any) -> Any:
        if self.tools is None:
            raise RuntimeError("tools unbound")
        return self.tools.call(caller, name, **kwargs)

    def persist_kv(self) -> None:
        self.store.set_kv(
            "meta",
            {
                "tick": self.tick,
                "now": iso(self.now),
                "frozen": sorted(self.frozen),
                "certified": self.certified,
                "identity": self.identity,
                "reputation": self.reputation.scores,
                "broker": self.broker.snapshot(),
                "last_prices": self.last_prices,
                "provider": self.router.snapshot(),
                "freeze_since": self.freeze_since,
            },
        )

    def load_kv(self) -> None:
        meta = self.store.get_kv("meta") or {}
        self.tick = int(meta.get("tick", 0))
        if meta.get("now"):
            self.now = datetime.fromisoformat(meta["now"])
        self.frozen = set(meta.get("frozen") or [])
        self.certified = list(meta.get("certified") or [])
        self.identity = dict(meta.get("identity") or {})
        if meta.get("reputation"):
            self.reputation.scores = {k: float(v) for k, v in meta["reputation"].items()}
        b = meta.get("broker") or {}
        if b:
            self.broker.cash = float(b.get("cash", self.broker.cash))
            self.broker.position = float(b.get("position", 0))
            self.broker.last_price = float(b.get("last_price", 0))
            self.broker.frozen = bool(b.get("frozen", False))
        self.last_prices = dict(meta.get("last_prices") or {})
        self.freeze_since = {k: int(v) for k, v in (meta.get("freeze_since") or {}).items()}

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


def bootstrap(config: EngineConfig) -> World:
    paths = config.paths()
    paths.ensure()
    seed_playbooks(paths.playbooks)
    store = Store(paths.db)
    ledger = Ledger(store)
    treasury = Treasury(ledger, config)
    wallet = Wallet(paths.secrets, paths.master_key)
    bundle_pub = wallet.load_or_create()
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
        identity={
            "name": config.firm_name,
            "eth": bundle_pub.eth_address,
            "sol": bundle_pub.sol_address,
            "mandate": config.mandate,
            "born": iso(),
        },
    )
    from sovereign.tools.catalog import build_registry

    world.tools = build_registry()
    world.tools.bind(world)
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
    from sovereign.heal.repair import setup as heal_setup

    heal_setup(world, full=False)
    world.persist_kv()
    return world


def load_prices(world: World) -> None:
    if world.market_close:
        return
    closes = None
    source = "synthetic"
    if world.config.mode == "live" and world.config.fetch_market_data:
        try:
            closes, source = fetch_closes()
            world.store.emit("market_fetch", {"source": source, "n": int(len(closes))}, "trader")
        except Exception as e:
            world.store.emit("market_fetch_failed", {"error": str(e)}, "trader")
    if closes is None:
        closes = synthetic_ohlc()
        source = "synthetic"
    world.market_close = [float(x) for x in closes]
    world.last_prices["BTCUSDT"] = float(closes[-1])
    world.last_prices["source"] = source


def ensure_certified(world: World) -> None:
    if world.certified:
        return
    load_prices(world)
    import numpy as np

    reports = certify(np.array(world.market_close, dtype=float), world.config.risk)
    world.certified = reports
    world.store.set_kv("certified", reports)
    artifact = world.config.paths().artifacts / "strategy_certification.json"
    artifact.write_text(json.dumps(reports, indent=2))
    for r in reports:
        world.store.emit("strategy_cert", r, "risk")
