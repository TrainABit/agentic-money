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
    # OS-level filesystem sandbox around jailed craft subprocesses:
    # "off" keeps the CLI tool allowlist only; "auto" wraps with bubblewrap
    # when it is installed; "bwrap" requires bubblewrap and fails closed.
    sandbox: Literal["off", "auto", "bwrap"] = "auto"
    # HTTP API fallback provider (used only when provider == "api", or when
    # allow_api_fallback is set and the Claude CLI is unavailable/failing).
    # The key is a vault credential reference, never a literal secret.
    api_style: Literal["anthropic", "openai"] = "anthropic"
    api_base_url: str = "https://api.anthropic.com/v1/messages"
    api_key_ref: str = "ANTHROPIC_API_KEY"
    api_timeout_s: float = 60.0


class SimConfig(BaseModel):
    """Closed-loop marketplace. Tests use defaults (fast settle)."""

    realism: bool = False
    autocollect: bool = True
    auto_accept: bool = True
    close_rate: float = 1.0
    pay_delay_ticks: int = 0
    daily_apply_cap: int = 8


class DebugConfig(BaseModel):
    """Tracing and slow-call thresholds for the debug instrumentation."""

    enabled: bool = False
    slow_tool_ms: float = 250.0
    trace_retention_files: int = 200
    include_tracebacks: bool = True


class WebConfig(BaseModel):
    """Headless web automation. Fail-closed: disabled and empty allowlist by
    default, so no agent can touch a browser until the operator opts in."""

    enabled: bool = False
    headless: bool = True
    allow_domains: tuple[str, ...] = ()
    max_actions: int = 25
    nav_timeout_ms: int = 30000
    block_media: bool = True
    actions_per_tick: int = 40


class McpServerConfig(BaseModel):
    """One external Model Context Protocol server the engine may bridge to.

    Fail-closed: an agent can only reach a server that names it in
    allow_agents, and env_credentials maps a child ENV VAR name to a vault
    credential ref that is resolved only at connect time (never stored here).
    """

    name: str
    transport: Literal["stdio", "http"] = "stdio"
    command: str | None = None
    args: tuple[str, ...] = ()
    url: str | None = None
    env_credentials: dict[str, str] = Field(default_factory=dict)
    allow_agents: tuple[str, ...] = ()
    allowed_tools: tuple[str, ...] = ()  # empty = all discovered tools
    timeout_s: float = 30.0
    calls_per_tick: int = 10


class McpConfig(BaseModel):
    """MCP client bridge. Fail-closed: disabled and no servers by default."""

    enabled: bool = False
    servers: tuple[McpServerConfig, ...] = ()


class ChainConfig(BaseModel):
    """On-chain settlement: prefer parsing transfer logs over balance deltas."""

    use_tx_logs: bool = True
    eth_lookback_blocks: int = 50_000
    eth_confirmations: int = 5
    sol_lookback_sigs: int = 200


class AlertConfig(BaseModel):
    """Push P0/P1 incidents (invariant breach, dead letters, halts) out of band."""

    enabled: bool = False
    channel: Literal["mail", "webhook"] = "mail"
    to: str = ""  # email recipient when channel == "mail"
    webhook_url: str | None = None
    min_severity: Literal["P0", "P1", "P2"] = "P0"
    throttle_minutes: float = 60.0


class LiveTiming(BaseModel):
    """Wall-clock lifecycle limits and bounded live cadences."""

    proposal_expiry_days: float = 14.0
    invoice_void_days: float = 90.0
    broker_cooldown_hours: float = 24.0
    agent_freeze_cooldown_hours: float = 24.0
    director_cadence_hours: float = 7 * 24.0
    improver_cadence_hours: float = 7 * 24.0
    publisher_cadence_hours: float = 24.0
    scout_cadence_hours: float = 24.0
    auditor_cadence_hours: float = 1.0
    full_heal_cadence_hours: float = 1.0
    price_refresh_hours: float = 1.0
    price_failure_retry_minutes: float = 5.0
    recertify_hours: float = 7 * 24.0
    certification_retry_hours: float = 1.0
    certification_failure_retry_minutes: float = 15.0
    craft_retry_hours: float = 1.0
    quorum_deadline_hours: float = 24.0
    mail_poll_minutes: float = 5.0


class WalletConfig(BaseModel):
    """Custody of the wallet's Fernet master key.

    "file" is today's default (master.key beside secrets.enc); "keyring"
    moves the key into the OS keyring (needs the optional [keyring] extra).
    """

    master_key_backend: Literal["file", "keyring"] = "file"
    keyring_service: str = "sovereign"
    keyring_username: str = "master_key"


class RetentionConfig(BaseModel):
    """Event/comms retention and compaction used by ``sovereign maintain``."""

    event_rows: int = 10_000
    comms_days: float = 30.0
    vacuum_on_maintain: bool = True


class TradingConfig(BaseModel):
    """Live trading venue. Sim always stays on the paper broker.

    Hyperliquid is fail-closed: ``hyperliquid_enabled`` defaults false,
    ``testnet`` defaults true, and mainnet additionally requires
    ``hyperliquid_allow_mainnet``. This engine never withdraws.
    """

    venue: Literal["paper", "hyperliquid"] = "paper"
    coin: str = "BTC"
    hyperliquid_enabled: bool = False
    hyperliquid_testnet: bool = True
    hyperliquid_allow_mainnet: bool = False
    slippage: float = 0.01
    min_order_usd: float = 12.0
    # Tests and local drills: never talks to Hyperliquid. Live default is false.
    hyperliquid_fake: bool = False


class WorkerConfig(BaseModel):
    """Optional multi-process agent execution.

    Off by default so sim tests stay single-process and deterministic.
    When enabled, agents not in ``in_process`` run in spawned workers
    that reopen the same SQLite world. Mechanic always stays in-process.
    """

    enabled: bool = False
    max_procs: int = 4
    in_process: tuple[str, ...] = ("mechanic", "courier")


class EngineConfig(BaseModel):
    mode: Mode = "sim"
    data_dir: Path = Field(default_factory=lambda: Path("data"))
    tick_seconds: float = 15.0
    # Live-mode sleep between ticks when the engine reports no active work.
    idle_tick_seconds: float = 60.0
    # Hard per-agent wall-clock budget: a wedged agent is abandoned so the
    # rest of the tick (and the firm) keeps running.
    agent_timeout_seconds: float = 30.0
    tick_hours: float = 24.0
    debug: DebugConfig = Field(default_factory=DebugConfig)
    web: WebConfig = Field(default_factory=WebConfig)
    mcp: McpConfig = Field(default_factory=McpConfig)
    chain: ChainConfig = Field(default_factory=ChainConfig)
    alerts: AlertConfig = Field(default_factory=AlertConfig)
    wallet: WalletConfig = Field(default_factory=WalletConfig)
    retention: RetentionConfig = Field(default_factory=RetentionConfig)
    trading: TradingConfig = Field(default_factory=TradingConfig)
    workers: WorkerConfig = Field(default_factory=WorkerConfig)
    goals: Goals = Field(default_factory=Goals)
    risk: RiskLimits = Field(default_factory=RiskLimits)
    models: ModelConfig = Field(default_factory=ModelConfig)
    sim: SimConfig = Field(default_factory=SimConfig)
    live_timing: LiveTiming = Field(default_factory=LiveTiming)
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
