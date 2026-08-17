from __future__ import annotations

import json
import shutil
import subprocess
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Protocol

from sovereign.config import EngineConfig, ModelTier


class Provider(Protocol):
    def complete(self, prompt: str, tier: ModelTier, system: str) -> str: ...

    def available(self) -> bool: ...

    def name(self) -> str: ...


@dataclass
class Usage:
    tokens: int = 0
    calls: int = 0
    by_tier: dict[str, int] | None = None

    def __post_init__(self) -> None:
        if self.by_tier is None:
            self.by_tier = {"fast": 0, "work": 0, "think": 0}


class SimBrain:
    """Deterministic language stand-in so the firm runs without a login."""

    def complete(self, prompt: str, tier: ModelTier, system: str) -> str:
        lowered = prompt.lower()
        if "proposal" in lowered or "cover letter" in lowered:
            return (
                "We will deliver a scoped, tested artifact in 48 hours. "
                "Fixed price, USDC or card. Scope is limited to what is written. "
                "Start today."
            )
        if "classify" in lowered or "json" in lowered:
            return '{"fit": 0.72, "reason": "matches automation/scripting skills"}'
        if "playbook" in lowered:
            return (
                "## Patch\n- Lead with a 3-line specific audit of their stack.\n"
                "- Quote a fixed price with a kill-scope sentence.\n"
                "- Prefer USDC prepay for new clients."
            )
        if "audit" in lowered:
            return '{"pass": true, "issues": [], "score": 80}'
        return f"[{tier}] acknowledged. Proceed with the deterministic plan."

    def available(self) -> bool:
        return True

    def name(self) -> str:
        return "sim-brain"


def jail_contains(child: Path, root: Path) -> bool:
    """True iff child is root or a descendant. Rejects prefix tricks like work-evil."""
    try:
        Path(child).resolve().relative_to(Path(root).resolve())
        return True
    except ValueError:
        return False


class ClaudeCodeProvider:
    def __init__(self, bin_name: str = "claude", models: dict[str, str] | None = None) -> None:
        self.bin_name = bin_name
        self.models = models or {"fast": "haiku", "work": "sonnet", "think": "opus"}

    def available(self) -> bool:
        return shutil.which(self.bin_name) is not None

    def name(self) -> str:
        return "claude-code"

    def _model(self, tier: ModelTier) -> str:
        return self.models.get(tier, {"fast": "haiku", "work": "sonnet", "think": "opus"}[tier])

    def complete(self, prompt: str, tier: ModelTier, system: str) -> str:
        if not self.available():
            raise RuntimeError("claude CLI not on PATH")
        full = f"{system.strip()}\n\n{prompt}"
        proc = subprocess.run(
            [
                self.bin_name,
                "-p",
                full,
                "--output-format",
                "text",
                "--model",
                self._model(tier),
            ],
            capture_output=True,
            text=True,
            timeout=180,
        )
        if proc.returncode != 0:
            raise RuntimeError(proc.stderr.strip() or "claude failed")
        return proc.stdout.strip()

    def complete_in_dir(
        self,
        prompt: str,
        cwd: Path,
        work_root: Path,
        tier: ModelTier = "work",
        timeout: int = 300,
    ) -> str:
        resolved = Path(cwd).resolve()
        root = Path(work_root).resolve()
        if not jail_contains(resolved, root):
            raise PermissionError("claude jail escape blocked")
        if not self.available():
            raise RuntimeError("claude CLI not on PATH")
        settings_dir = resolved / ".claude"
        settings_dir.mkdir(exist_ok=True)
        (settings_dir / "settings.json").write_text(
            json.dumps(
                {
                    "permissions": {
                        "allow": ["Read", "Write", "Edit", "Glob", "Grep"],
                        "deny": ["Bash", "Agent", "WebFetch", "WebSearch"],
                    }
                }
            )
        )
        proc = subprocess.run(
            [
                self.bin_name,
                "-p",
                prompt,
                "--output-format",
                "text",
                "--model",
                self._model(tier),
                "--permission-mode",
                "dontAsk",
                "--allowedTools",
                "Read,Write,Edit,Glob,Grep",
                "--disallowedTools",
                "Bash,Agent,WebFetch,WebSearch",
            ],
            capture_output=True,
            text=True,
            timeout=timeout,
            cwd=str(resolved),
        )
        if proc.returncode != 0:
            raise RuntimeError(proc.stderr.strip() or "claude failed")
        return proc.stdout.strip()


class Router:
    def __init__(self, config: EngineConfig) -> None:
        self.config = config
        self.sim = SimBrain()
        self.claude = ClaudeCodeProvider(
            config.models.claude_bin,
            models={"fast": config.models.fast, "work": config.models.work, "think": config.models.think},
        )
        self.usage = Usage()
        self.usage_day = ""
        self.degraded = False
        self.queued = 0

    def provider_name(self) -> str:
        if self.degraded:
            return "degraded"
        if self.config.mode == "sim":
            return self.sim.name()
        if self.config.models.provider == "claude_code" and self.claude.available():
            return self.claude.name()
        return self.sim.name()

    def _roll_day(self) -> None:
        day = datetime.now(timezone.utc).date().isoformat()
        if self.usage_day != day:
            self.usage = Usage()
            self.usage_day = day
            self.degraded = False

    def remaining_budget(self) -> int:
        self._roll_day()
        return max(0, self.config.models.daily_token_budget - self.usage.tokens)

    def complete(
        self,
        prompt: str,
        tier: ModelTier = "fast",
        system: str = "You are a Sovereign firm agent. Be specific. No fluff.",
    ) -> str:
        est = max(32, (len(system) + len(prompt)) // 4 + 256)
        if tier == "think" and self.usage.tokens > 0.7 * self.config.models.daily_token_budget:
            tier = "work"
        if est > self.remaining_budget():
            self.degraded = True
            self.queued += 1
            if self.config.mode == "live":
                return ""
            text = self.sim.complete(prompt, "fast", system)
            self._count("fast", len(text) // 4)
            return text
        if self.config.mode == "live" and self.claude.available():
            try:
                text = self.claude.complete(prompt, tier, system)
                self._count(tier, est + len(text) // 4)
                return text
            except Exception:
                self.degraded = True
                self.queued += 1
                if not self.config.models.allow_api_fallback:
                    return ""
                raise
        text = self.sim.complete(prompt, tier, system)
        self._count("fast", len(text) // 4)
        return text

    def complete_in_dir(self, prompt: str, cwd: Path, work_root: Path, tier: ModelTier = "work") -> str:
        if self.config.mode == "live" and self.claude.available() and not self.degraded:
            try:
                text = self.claude.complete_in_dir(prompt, cwd, work_root, tier=tier)
                self._count(tier, max(32, len(prompt) // 4 + 256))
                return text
            except Exception:
                self.degraded = True
                return self.sim.complete(prompt, tier, "jailed crafter")
        return self.sim.complete(prompt, tier, "jailed crafter")

    def _count(self, tier: str, tokens: int) -> None:
        self._roll_day()
        self.usage.tokens += tokens
        self.usage.calls += 1
        assert self.usage.by_tier is not None
        self.usage.by_tier[tier] = self.usage.by_tier.get(tier, 0) + tokens

    def snapshot(self) -> dict[str, Any]:
        self._roll_day()
        return {
            "provider": self.provider_name(),
            "tokens": self.usage.tokens,
            "calls": self.usage.calls,
            "by_tier": self.usage.by_tier,
            "budget": self.config.models.daily_token_budget,
            "claude_cli": self.claude.available(),
            "usage_day": self.usage_day,
            "degraded": self.degraded,
            "queued": self.queued,
        }

    def restore(self, snap: dict[str, Any]) -> None:
        self.usage_day = str(snap.get("usage_day") or "")
        self.degraded = bool(snap.get("degraded"))
        self.queued = int(snap.get("queued") or 0)
        self.usage.tokens = int(snap.get("tokens") or 0)
        self.usage.calls = int(snap.get("calls") or 0)
        if snap.get("by_tier"):
            self.usage.by_tier = dict(snap["by_tier"])
        self._roll_day()
