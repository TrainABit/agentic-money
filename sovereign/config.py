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


class EngineConfig(BaseModel):
    mode: Mode = "sim"
    data_dir: Path = Field(default_factory=lambda: Path("data"))
    tick_seconds: float = 15.0
    goals: Goals = Field(default_factory=Goals)
    risk: RiskLimits = Field(default_factory=RiskLimits)
    models: ModelConfig = Field(default_factory=ModelConfig)
    firm_name: str = "Northline Autonomous"
    mandate: str = (
        "Act in the operator's commercial name. Earn, deliver, collect, compound. "
        "No fraud, spam, manipulation, or KYC evasion. Humans supply logins only."
    )
    public_job_apis: bool = True
    fetch_market_data: bool = True

    def paths(self) -> "Paths":
        return Paths(self.data_dir)


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

    def ensure(self) -> None:
        for p in (
            self.root,
            self.logs,
            self.playbooks,
            self.work,
            self.deliveries,
            self.artifacts,
        ):
            p.mkdir(parents=True, exist_ok=True)
