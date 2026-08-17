from __future__ import annotations

from pathlib import Path
from typing import Literal

from pydantic import BaseModel, Field


Mode = Literal["sim", "live"]
ModelTier = Literal["fast", "work", "think"]


class Goals(BaseModel):
    minimum_usd: float = 2000.0
    recommended_usd: float = 5000.0
    good_usd: float = 7000.0


class RiskLimits(BaseModel):
    hot_wallet_cap_usd: float = 1500.0
    trading_risk_per_signal: float = 0.01
    daily_halt_pct: float = 0.03
    weekly_halt_pct: float = 0.07
    max_leverage: float = 2.0
    min_sharpe_oos: float = 0.5
    max_drawdown_oos: float = 0.35
    min_trades_oos: int = 10
    round_trip_cost: float = 0.001
    operating_cash_is_tradable: bool = False


class ModelConfig(BaseModel):
    provider: Literal["claude_code", "sim", "api"] = "claude_code"
    allow_api_fallback: bool = False
    fast: str = "haiku"
    work: str = "sonnet"
    think: str = "opus"
    daily_token_budget: int = 400_000
    claude_bin: str = "claude"


class SimConfig(BaseModel):
    """Closed-loop marketplace. Tests use defaults (fast settle)."""

    realism: bool = False
    autocollect: bool = True
    auto_accept: bool = True
    close_rate: float = 1.0
    pay_delay_ticks: int = 0
    daily_apply_cap: int = 8


class EngineConfig(BaseModel):
    mode: Mode = "sim"
    data_dir: Path = Field(default_factory=lambda: Path("data"))
    tick_seconds: float = 15.0
    tick_hours: float = 24.0
    goals: Goals = Field(default_factory=Goals)
    risk: RiskLimits = Field(default_factory=RiskLimits)
    models: ModelConfig = Field(default_factory=ModelConfig)
    sim: SimConfig = Field(default_factory=SimConfig)
    firm_name: str = "Northline Autonomous"
    mandate: str = (
        "Act in the operator's commercial name. Earn, deliver, collect, compound. "
        "No fraud, spam, manipulation, or KYC evasion. Humans supply logins only."
    )
    public_job_apis: bool = True
    fetch_market_data: bool = True
    rpc_url: str = "https://ethereum.publicnode.com"
    usdc_token: str = "0xA0b86991c6218b36c1d19D4a2e9Eb0cE3606eB48"
    sol_rpc_url: str = "https://api.mainnet-beta.solana.com"
    sol_usdc_mint: str = "EPjFWdd5AufqSSqeM2qN1xzybapC8G4wEGGkZwyTDt1v"
    allow_live_infra_buy: bool = False
    daily_apply_cap: int = 8

    def paths(self) -> "Paths":
        return Paths(self.data_dir)

    def autocollect(self) -> bool:
        if self.mode == "live":
            return False
        if self.sim.realism:
            return False
        return self.sim.autocollect

    def auto_accept(self) -> bool:
        if self.mode == "live":
            return False
        if self.sim.realism:
            return False
        return self.sim.auto_accept

    def price_refresh_every(self) -> int:
        if self.mode == "sim":
            return 10**9
        return max(1, int(3600 / max(self.tick_seconds, 1)))

    def recertify_every(self) -> int:
        if self.mode == "sim":
            return 7
        return max(1, int(7 * 24 * 3600 / max(self.tick_seconds, 1)))

    def apply_cap(self) -> int:
        if self.mode == "sim":
            return self.sim.daily_apply_cap
        return self.daily_apply_cap


class Paths:
    def __init__(self, root: Path) -> None:
        self.root = root
        self.db = root / "sovereign.db"
        self.world = root / "world.json"
        self.secrets = root / "secrets.enc"
        self.master_key = root / "master.key"
        self.logs = root / "logs"
        self.playbooks = root / "playbooks"
        self.work = root / "work"
        self.deliveries = root / "deliveries"
        self.artifacts = root / "artifacts"
        self.human = root / "human_inbox.json"
        self.human_replies = root / "human_replies.json"
        self.mail_outbox = root / "mail" / "outbox"
        self.mail_inbox = root / "mail" / "inbox"
        self.mail_sent = root / "mail" / "sent"
        self.invoices = root / "invoices"
        self.lock = root / "engine.lock"
        self.config_yaml = root / "config.yaml"

    def ensure(self) -> None:
        for p in (
            self.root,
            self.logs,
            self.playbooks,
            self.work,
            self.deliveries,
            self.artifacts,
            self.mail_outbox,
            self.mail_inbox,
            self.mail_sent,
            self.invoices,
        ):
            p.mkdir(parents=True, exist_ok=True)
