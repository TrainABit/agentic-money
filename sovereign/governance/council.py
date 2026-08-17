from __future__ import annotations

from typing import Any

from sovereign.memory.store import Store


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
