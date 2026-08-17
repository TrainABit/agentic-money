from __future__ import annotations

from typing import Any


class Reputation:
    def __init__(self, scores: dict[str, float] | None = None) -> None:
        self.scores = scores or {}

    def get(self, agent: str) -> float:
        return float(self.scores.get(agent, 70.0))

    def slash(self, agent: str, pts: float, reason: str) -> dict[str, Any]:
        self.scores[agent] = max(0.0, self.get(agent) - pts)
        return {"agent": agent, "score": self.get(agent), "reason": reason, "delta": -pts}

    def boost(self, agent: str, pts: float, reason: str) -> dict[str, Any]:
        self.scores[agent] = min(100.0, self.get(agent) + pts)
        return {"agent": agent, "score": self.get(agent), "reason": reason, "delta": pts}

    def autonomy_usd(self, agent: str, base: float = 250.0) -> float:
        return round(base * (self.get(agent) / 100.0), 2)

    def should_freeze(self, agent: str) -> bool:
        return self.get(agent) < 20.0
