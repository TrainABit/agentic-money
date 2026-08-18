from __future__ import annotations

import os
import shutil
import subprocess
import tempfile
from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Protocol

import httpx

from sovereign.config import EngineConfig, ModelConfig, ModelTier

SecretResolver = Callable[[str], str | None]


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


class ApiProvider:
    """Direct HTTP completion API (Anthropic Messages or OpenAI chat style).

    Text-only: it cannot run tools, so jailed crafting never routes here. The
    api key is resolved per call (vault resolver first, process env fallback),
    never stored on the instance, and never included in error messages.
    """

    MAX_COMPLETION_TOKENS = 4096

    def __init__(self, config: ModelConfig, secret_resolver: SecretResolver | None = None) -> None:
        self.config = config
        self.secret_resolver = secret_resolver

    def _key(self) -> str:
        ref = self.config.api_key_ref
        if self.secret_resolver is not None:
            try:
                resolved = self.secret_resolver(ref)
            except Exception:  # noqa: BLE001 - a broken vault must not crash status paths
                resolved = None
            if resolved:
                return str(resolved)
        return os.environ.get(ref) or ""

    def available(self) -> bool:
        return bool(self._key())

    def name(self) -> str:
        return f"api:{self.config.api_style}"

    def _model(self, tier: ModelTier) -> str:
        return {"fast": self.config.fast, "work": self.config.work, "think": self.config.think}[tier]

    def complete(self, prompt: str, tier: ModelTier, system: str) -> str:
        return self.complete_with_usage(prompt, tier, system)[0]

    def complete_with_usage(self, prompt: str, tier: ModelTier, system: str) -> tuple[str, int | None]:
        """Return (text, real total tokens) — tokens is None when usage is absent."""
        key = self._key()
        if not key:
            raise RuntimeError(f"no api key resolvable for ref {self.config.api_key_ref!r}")
        if self.config.api_style == "anthropic":
            headers = {
                "x-api-key": key,
                "anthropic-version": "2023-06-01",
                "content-type": "application/json",
            }
            body: dict[str, Any] = {
                "model": self._model(tier),
                "max_tokens": self.MAX_COMPLETION_TOKENS,
                "system": system,
                "messages": [{"role": "user", "content": prompt}],
            }
        else:
            headers = {
                "authorization": f"Bearer {key}",
                "content-type": "application/json",
            }
            body = {
                "model": self._model(tier),
                "messages": [
                    {"role": "system", "content": system},
                    {"role": "user", "content": prompt},
                ],
            }
        try:
            response = httpx.post(
                self.config.api_base_url,
                json=body,
                headers=headers,
                timeout=self.config.api_timeout_s,
            )
        except httpx.HTTPError as exc:
            # `from None`: httpx exceptions reference the request (and its headers).
            raise RuntimeError(f"api request failed: {type(exc).__name__}") from None
        if response.status_code != 200:
            raise RuntimeError(f"api returned HTTP {response.status_code}")
        try:
            data = response.json()
            if self.config.api_style == "anthropic":
                text = str(data["content"][0]["text"])
            else:
                text = str(data["choices"][0]["message"]["content"])
        except Exception:  # noqa: BLE001 - response bodies must never leak into errors
            raise RuntimeError("api response body was malformed") from None
        return text, self._usage_total(data)

    def _usage_total(self, data: Any) -> int | None:
        try:
            usage = data.get("usage") or {}
            if self.config.api_style == "anthropic":
                total = int(usage.get("input_tokens") or 0) + int(usage.get("output_tokens") or 0)
            else:
                total = int(usage.get("total_tokens") or 0)
        except Exception:  # noqa: BLE001 - usage is best-effort; the text is the contract
            return None
        return total if total > 0 else None


def jail_contains(child: Path, root: Path) -> bool:
    """True iff child is root or a descendant. Rejects prefix tricks like work-evil."""
    try:
        Path(child).resolve().relative_to(Path(root).resolve())
        return True
    except ValueError:
        return False


def sandbox_argv(argv: list[str], workdir: Path, *, home: Path | None = None) -> list[str]:
    """Wrap a jailed subprocess in bubblewrap: whole FS read-only, job dir writable.

    Network stays shared (the CLI talks to its provider), /tmp is a private
    tmpfs, and only the job workdir plus the CLI's own session state under the
    user's home are writable. This contains filesystem writes even if the
    subprocess's internal tool policy fails.
    """
    home = home or Path.home()
    wrapped = [
        "bwrap",
        "--die-with-parent",
        "--new-session",
        "--unshare-pid",
        "--ro-bind", "/", "/",
        "--dev", "/dev",
        "--proc", "/proc",
        "--tmpfs", "/tmp",
        "--bind", str(workdir), str(workdir),
    ]
    for session_path in (home / ".claude", home / ".claude.json"):
        if session_path.exists():
            wrapped += ["--bind", str(session_path), str(session_path)]
    return wrapped + ["--", *argv]


class ClaudeCodeProvider:
    MAX_OUTPUT_BYTES = 1024 * 1024
    ALL_TOOLS = ("Read", "Write", "Edit", "Glob", "Grep", "Bash", "Agent", "WebFetch", "WebSearch")
    CRAFT_TOOLS = ("Read", "Write", "Edit", "Glob", "Grep")
    CRAFT_DENIED_TOOLS = ("Bash", "Agent", "WebFetch", "WebSearch")

    def __init__(
        self,
        bin_name: str = "claude",
        models: dict[str, str] | None = None,
        sandbox: str = "auto",
    ) -> None:
        self.bin_name = bin_name
        self.models = models or {"fast": "haiku", "work": "sonnet", "think": "opus"}
        self.sandbox = sandbox

    def _sandboxed(self, argv: list[str], workdir: Path) -> list[str]:
        if self.sandbox == "off":
            return argv
        bwrap_available = shutil.which("bwrap") is not None
        if not bwrap_available:
            if self.sandbox == "bwrap":
                raise RuntimeError("sandbox mode 'bwrap' requires bubblewrap on PATH")
            return argv
        return sandbox_argv(argv, workdir)

    def available(self) -> bool:
        return shutil.which(self.bin_name) is not None

    def name(self) -> str:
        return "claude-code"

    def _model(self, tier: ModelTier) -> str:
        return self.models.get(tier, {"fast": "haiku", "work": "sonnet", "think": "opus"}[tier])

    def _bounded_output(self, handle: Any, fallback: Any = None) -> str:
        handle.seek(0)
        raw = handle.read(self.MAX_OUTPUT_BYTES + 1)
        if not raw and fallback:
            raw = fallback.encode() if isinstance(fallback, str) else bytes(fallback)
            raw = raw[: self.MAX_OUTPUT_BYTES + 1]
        truncated = len(raw) > self.MAX_OUTPUT_BYTES
        raw = raw[: self.MAX_OUTPUT_BYTES]
        text = raw.decode("utf-8", errors="replace")
        return text + ("\n[output truncated]" if truncated else "")

    def _invoke(self, argv: list[str], *, cwd: Path, timeout: int) -> str:
        with tempfile.TemporaryFile(mode="w+b") as stdout, tempfile.TemporaryFile(mode="w+b") as stderr:
            try:
                proc = subprocess.run(
                    argv,
                    stdout=stdout,
                    stderr=stderr,
                    stdin=subprocess.DEVNULL,
                    timeout=timeout,
                    cwd=str(cwd),
                    check=False,
                )
            except subprocess.TimeoutExpired as exc:
                raise RuntimeError("claude timed out") from exc
            output = self._bounded_output(stdout, getattr(proc, "stdout", None))
            error = self._bounded_output(stderr, getattr(proc, "stderr", None))
        if proc.returncode != 0:
            raise RuntimeError(error.strip() or "claude failed")
        return output.strip()

    def complete(self, prompt: str, tier: ModelTier, system: str) -> str:
        if not self.available():
            raise RuntimeError("claude CLI not on PATH")
        full = f"{system.strip()}\n\n{prompt}"
        argv = [
            self.bin_name,
            "-p",
            full,
            "--output-format",
            "text",
            "--model",
            self._model(tier),
            "--permission-mode",
            "dontAsk",
            "--allowedTools",
            "",
            "--disallowedTools",
            ",".join(self.ALL_TOOLS),
        ]
        with tempfile.TemporaryDirectory(prefix="sovereign-claude-") as directory:
            return self._invoke(argv, cwd=Path(directory), timeout=180)

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
        argv = [
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
            ",".join(self.CRAFT_TOOLS),
            "--disallowedTools",
            "Bash,Agent,WebFetch,WebSearch",
        ]
        return self._invoke(self._sandboxed(argv, resolved), cwd=resolved, timeout=timeout)


class Router:
    def __init__(self, config: EngineConfig, secret_resolver: SecretResolver | None = None) -> None:
        self.config = config
        self.sim = SimBrain()
        self.claude = ClaudeCodeProvider(
            config.models.claude_bin,
            models={"fast": config.models.fast, "work": config.models.work, "think": config.models.think},
            sandbox=config.models.sandbox,
        )
        self.api = ApiProvider(config.models, secret_resolver)
        self.usage = Usage()
        self.usage_day = ""
        self.degraded = False
        self.queued = 0
        self.last_error: str | None = None

    def provider_name(self) -> str:
        if self.config.mode == "sim":
            return self.sim.name()
        models = self.config.models
        if models.provider == "api" and self.api.available():
            return self.api.name()
        if models.provider == "claude_code" and self.claude.available():
            return self.claude.name()
        if self.degraded:
            return "degraded"
        return "unavailable"

    def _roll_day(self) -> None:
        day = datetime.now(UTC).date().isoformat()
        if self.usage_day != day:
            self.usage = Usage()
            self.usage_day = day
            self.degraded = False
            self.last_error = None

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
        self._roll_day()
        if tier == "think" and self.usage.tokens > 0.7 * self.config.models.daily_token_budget:
            tier = "work"
        if est > self.remaining_budget():
            if self.config.mode == "live":
                return self._queue("daily model budget exhausted")
            text = self.sim.complete(prompt, "fast", system)
            self._count("fast", len(text) // 4)
            return text
        if self.config.mode != "live":
            text = self.sim.complete(prompt, tier, system)
            self._count("fast", len(text) // 4)
            return text
        if self.degraded:
            return self._queue(self.last_error or "model provider unavailable")
        models = self.config.models
        if models.provider == "api":
            if not self.api.available():
                return self._queue(f"api key {models.api_key_ref!r} is not resolvable")
            return self._api_complete(prompt, tier, system, est)
        if models.provider != "claude_code":
            return self._queue(f"model provider {models.provider!r} is unavailable")
        if not self.claude.available():
            return self._claude_fallback(prompt, tier, system, est, "claude CLI unavailable")
        try:
            text = self.claude.complete(prompt, tier, system)
        except Exception as exc:  # noqa: BLE001 - live inference must fail closed
            return self._claude_fallback(
                prompt, tier, system, est, f"claude invocation failed: {str(exc)[:120]}"
            )
        self._count(tier, est + len(text) // 4)
        return text

    def _api_complete(self, prompt: str, tier: ModelTier, system: str, est: int) -> str:
        try:
            text, real_tokens = self.api.complete_with_usage(prompt, tier, system)
        except Exception as exc:  # noqa: BLE001 - live inference must fail closed
            return self._queue(f"api invocation failed: {str(exc)[:120]}")
        self._count(tier, real_tokens if real_tokens is not None else est + len(text) // 4)
        return text

    def _claude_fallback(self, prompt: str, tier: ModelTier, system: str, est: int, reason: str) -> str:
        models = self.config.models
        if models.allow_api_fallback and self.api.available():
            return self._api_complete(prompt, tier, system, est)
        if models.allow_api_fallback:
            reason += "; API fallback is not configured"
        else:
            reason += "; API fallback disabled"
        return self._queue(reason)

    def complete_in_dir(self, prompt: str, cwd: Path, work_root: Path, tier: ModelTier = "work") -> str:
        if not jail_contains(Path(cwd), Path(work_root)):
            raise PermissionError("claude jail escape blocked")
        estimate = max(32, len(prompt) // 4 + 256)
        if self.config.mode != "live":
            text = self.sim.complete(prompt, tier, "jailed crafter")
            self._count("fast", len(text) // 4)
            return text
        if estimate > self.remaining_budget():
            return self._queue("daily model budget exhausted")
        if self.degraded:
            return self._queue(self.last_error or "model provider unavailable")
        # Jailed crafting needs a tool-running provider; the HTTP API is
        # text-only, so this path is claude_code-only and never falls back.
        if self.config.models.provider != "claude_code":
            return self._queue(
                f"model provider {self.config.models.provider!r} cannot run jailed crafting; "
                "claude_code required"
            )
        if not self.claude.available():
            reason = "claude CLI unavailable"
            if self.config.models.allow_api_fallback:
                reason += "; API fallback cannot run jailed crafting"
            return self._queue(reason)
        try:
            text = self.claude.complete_in_dir(prompt, cwd, work_root, tier=tier)
        except Exception as exc:  # noqa: BLE001 - live inference must fail closed
            reason = f"claude invocation failed: {str(exc)[:120]}"
            if self.config.models.allow_api_fallback:
                reason += "; API fallback cannot run jailed crafting"
            return self._queue(reason)
        self._count(tier, estimate + len(text) // 4)
        return text

    def _queue(self, reason: str) -> str:
        self.degraded = True
        self.queued += 1
        self.last_error = reason
        return ""

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
            "api_configured": self.api.available(),
            "usage_day": self.usage_day,
            "degraded": self.degraded,
            "queued": self.queued,
            "last_error": self.last_error,
        }

    def restore(self, snap: dict[str, Any]) -> None:
        self.usage_day = str(snap.get("usage_day") or "")
        self.degraded = bool(snap.get("degraded"))
        self.queued = int(snap.get("queued") or 0)
        self.last_error = str(snap.get("last_error")) if snap.get("last_error") else None
        self.usage.tokens = int(snap.get("tokens") or 0)
        self.usage.calls = int(snap.get("calls") or 0)
        if snap.get("by_tier"):
            self.usage.by_tier = dict(snap["by_tier"])
        self._roll_day()
