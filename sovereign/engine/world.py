from __future__ import annotations

import json
from dataclasses import dataclass, field
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
    frozen: set[str] = field(default_factory=set)
    certified: list[dict[str, Any]] = field(default_factory=list)
    identity: dict[str, str] = field(default_factory=dict)
    market_close: list[float] = field(default_factory=list)
    last_prices: dict[str, float] = field(default_factory=dict)

    def persist_kv(self) -> None:
        self.store.set_kv(
            "meta",
            {
                "tick": self.tick,
                "frozen": sorted(self.frozen),
                "certified": self.certified,
                "identity": self.identity,
                "reputation": self.reputation.scores,
                "broker": self.broker.snapshot(),
                "last_prices": self.last_prices,
                "provider": self.router.snapshot(),
            },
        )

    def load_kv(self) -> None:
        meta = self.store.get_kv("meta") or {}
        self.tick = int(meta.get("tick", 0))
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

    def status(self) -> dict[str, Any]:
        snap = self.ledger.snapshot()
        goals = self.config.goals
        rev = snap["revenue_usd"]
        return {
            "firm": self.config.firm_name,
            "mode": self.config.mode,
            "tick": self.tick,
            "ts": iso(),
            "identity": self.identity,
            "wallet": self.wallet.public(),
            "ledger": snap,
            "treasury": self.treasury.policy_status(),
            "goals": {
                "minimum": goals.minimum_usd,
                "recommended": goals.recommended_usd,
                "good": goals.good_usd,
                "run_rate_usd": rev,
                "progress_min": round(min(1.0, rev / goals.minimum_usd), 4),
                "progress_rec": round(min(1.0, rev / goals.recommended_usd), 4),
                "progress_good": round(min(1.0, rev / goals.good_usd), 4),
            },
            "certified_strategies": [c for c in self.certified if c.get("certified")],
            "rejected_strategies": [c for c in self.certified if not c.get("certified")],
            "broker": self.broker.snapshot(),
            "frozen_agents": sorted(self.frozen),
            "reputation": self.reputation.scores,
            "open_jobs": len(self.store.jobs("open")),
            "active_jobs": len(self.store.jobs("accepted")) + len(self.store.jobs("in_progress")),
            "human_inbox": self.human.open(),
            "cognition": self.router.snapshot(),
            "recent_events": self.store.events(25),
            "missions": self.store.missions(),
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
        # Genesis float in sim so the loop can pay tiny infra and show compounding
        if config.mode == "sim":
            ledger.post("assets.usdc", "equity.treasury", 250.0, "sim genesis float")
        world.human.ask(
            service="claude",
            instruction="Install Claude Code if needed, then run `claude login` on the host using the Claude Pro/Max subscription (not an API key). Reply with {\"ok\": \"1\"}.",
            fields=["ok"],
            why="Live cognition uses the subscription CLI. Sim brain already runs.",
        )
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
    world.market_close = [float(x) for x in closes]
    world.last_prices["BTCUSDT"] = float(closes[-1])


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
