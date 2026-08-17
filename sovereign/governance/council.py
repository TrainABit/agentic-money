from __future__ import annotations

from typing import TYPE_CHECKING

from sovereign.memory.store import Store

if TYPE_CHECKING:
    from sovereign.engine.world import World


def treasurer_vote(world: "World", usd: float) -> tuple[str, str]:
    """Treasurer seat: the spend must fit operating cash and either the
    operator's earned autonomy or the $25 petty-cash floor. Same semantics as
    the treasurer leg of :meth:`Council.auto_votes_for_spend`."""
    operating_cash = world.treasury.operating_cash()
    if usd > operating_cash:
        return "no", "exceeds operating cash"
    autonomy = max(world.reputation.autonomy_usd("operator", 80), 0)
    if usd <= autonomy or usd <= 25:
        return "yes", "within operating cash and autonomy"
    return "no", "exceeds operator autonomy"


def risk_vote(world: "World", usd: float) -> tuple[str, str]:
    """Risk seat: no while the operator is frozen or the spend would consume
    more than a quarter of operating cash. Same semantics as the risk leg of
    :meth:`Council.auto_votes_for_spend`."""
    if "operator" in world.frozen:
        return "no", "operator frozen"
    if usd > world.treasury.operating_cash() * 0.25:
        return "no", "exceeds 25% of operating cash"
    return "yes", "limits intact"


def director_vote(world: "World") -> tuple[str, str]:
    """Director seat: yes only when trailing 30-day revenue is positive."""
    trailing = world.ledger.snapshot(now=world.now)["trailing_30d_usd"]
    if trailing > 0:
        return "yes", "trailing revenue positive"
    return "no", "no trailing revenue"


class Council:
    """Agents control agents. Humans are not on this council."""

    REQUIRED = {
        "spend": ("treasurer", "risk"),
        "live_trade": ("risk",),
        "buy_infra": ("treasurer", "director"),
        "promote_playbook": ("auditor",),
        "unfreeze": ("risk", "director"),
        "remove_director": ("treasurer", "risk", "auditor"),
    }

    def __init__(self, store: Store) -> None:
        self.store = store

    def vote(self, action_id: str, agent: str, choice: str, reason: str) -> None:
        self.store.insert_vote(action_id, agent, choice, reason)

    def quorum(self, action_id: str, kind: str, votes: dict[str, str]) -> tuple[bool, str]:
        need = self.REQUIRED.get(kind, ())
        for seat in need:
            if votes.get(seat) != "yes":
                return False, f"missing yes from {seat}"
        for agent, choice in votes.items():
            self.vote(action_id, agent, choice, kind)
        return True, "quorum"

    def auto_votes_for_spend(
        self,
        usd: float,
        operating_cash: float,
        frozen: bool,
        autonomy: float,
    ) -> dict[str, str]:
        treasurer = "yes" if usd <= operating_cash and usd <= max(autonomy, 0) else "no"
        if usd <= 25 and usd <= operating_cash:
            treasurer = "yes"
        risk = "no" if frozen else "yes"
        if usd > operating_cash * 0.25:
            risk = "no"
        return {"treasurer": treasurer, "risk": risk}
