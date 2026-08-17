from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class Play:
    id: str
    title: str
    thesis: str
    agents: tuple[str, ...]
    attention_until_min: float
    attention_until_rec: float
    attention_after_rec: float
    monthly_target_usd: float
    kill_after_days_if_zero: int = 14


PLAYS: tuple[Play, ...] = (
    Play(
        id="labor_studio",
        title="Autonomous labor studio",
        thesis="Find, win, ship, and collect small remote jobs. Fastest path to $2k.",
        agents=("hunter", "closer", "crafter", "bookkeeper", "auditor"),
        attention_until_min=0.70,
        attention_until_rec=0.45,
        attention_after_rec=0.25,
        monthly_target_usd=2500,
    ),
    Play(
        id="productized",
        title="Productized service + retainers",
        thesis="Name a repeatable offer with a fixed price and SLA. Path to $5k.",
        agents=("scout", "publisher", "closer", "crafter", "treasurer"),
        attention_until_min=0.15,
        attention_until_rec=0.30,
        attention_after_rec=0.35,
        monthly_target_usd=2500,
    ),
    Play(
        id="digital_products",
        title="Digital products",
        thesis="Package internal tools and sell with near-zero marginal delivery.",
        agents=("crafter", "publisher", "scout"),
        attention_until_min=0.05,
        attention_until_rec=0.10,
        attention_after_rec=0.15,
        monthly_target_usd=800,
        kill_after_days_if_zero=21,
    ),
    Play(
        id="tsmom_crypto",
        title="Certified crypto momentum",
        thesis="Vol-targeted time-series momentum on BTC/ETH. Compounds treasury; does not pay rent from dust.",
        agents=("trader", "risk", "auditor", "treasurer"),
        attention_until_min=0.05,
        attention_until_rec=0.10,
        attention_after_rec=0.15,
        monthly_target_usd=400,
        kill_after_days_if_zero=30,
    ),
    Play(
        id="b2b_outbound",
        title="SMB automation retainers",
        thesis="Sell done-for-you automations to small businesses. High ceiling, needs mail domain.",
        agents=("scout", "closer", "crafter", "courier", "operator"),
        attention_until_min=0.03,
        attention_until_rec=0.03,
        attention_after_rec=0.07,
        monthly_target_usd=1500,
    ),
    Play(
        id="infra_arb",
        title="Infra, domains, affiliates",
        thesis="Buy cheap compute with crypto, host products, take only ethical affiliates.",
        agents=("operator", "scout", "publisher"),
        attention_until_min=0.02,
        attention_until_rec=0.02,
        attention_after_rec=0.03,
        monthly_target_usd=300,
        kill_after_days_if_zero=21,
    ),
)


def attention_map(run_rate_usd: float, minimum: float, recommended: float) -> dict[str, float]:
    out: dict[str, float] = {}
    for p in PLAYS:
        if run_rate_usd < minimum:
            out[p.id] = p.attention_until_min
        elif run_rate_usd < recommended:
            out[p.id] = p.attention_until_rec
        else:
            out[p.id] = p.attention_after_rec
    s = sum(out.values()) or 1.0
    return {k: v / s for k, v in out.items()}
