"""HTTP API model provider: routing, fallback, secrecy, and accounting.

All httpx traffic is mocked at sovereign.runtime.router.httpx.post — no test
here may touch the network. Key refs use TEST_-prefixed env var names so a
developer's real ANTHROPIC/OPENAI keys can never leak into assertions.
"""

from __future__ import annotations

import json

import pytest

from sovereign.config import EngineConfig
from sovereign.runtime.router import ApiProvider, Router

SECRET = "sk-secret-vault-xyzzy-0451"
ANTHROPIC_REF = "TEST_SOVEREIGN_ANTHROPIC_KEY"
OPENAI_REF = "TEST_SOVEREIGN_OPENAI_KEY"

ANTHROPIC_PAYLOAD = {
    "content": [{"text": "anthropic says hi"}],
    "usage": {"input_tokens": 100, "output_tokens": 23},
}
OPENAI_PAYLOAD = {
    "choices": [{"message": {"content": "openai says hi"}}],
    "usage": {"total_tokens": 77},
}


class FakeResponse:
    def __init__(self, status_code: int = 200, payload: dict | None = None) -> None:
        self.status_code = status_code
        self._payload = payload if payload is not None else {}

    def json(self) -> dict:
        return self._payload


def live_config(tmp_path, **models) -> EngineConfig:
    cfg = EngineConfig(
        mode="live",
        data_dir=tmp_path,
        public_job_apis=False,
        fetch_market_data=False,
    )  # type: ignore[arg-type]
    for key, value in models.items():
        setattr(cfg.models, key, value)
    return cfg


def patch_post(monkeypatch, response: FakeResponse):
    """Route router httpx.post to a canned response; return the call log."""
    calls: list[dict] = []

    def fake_post(url, *, json=None, headers=None, timeout=None):
        calls.append({"url": url, "json": json, "headers": headers, "timeout": timeout})
        return response

    monkeypatch.setattr("sovereign.runtime.router.httpx.post", fake_post)
    return calls


def forbid_post(monkeypatch):
    def boom(*_args, **_kwargs):
        raise AssertionError("httpx.post must not be called in this scenario")

    monkeypatch.setattr("sovereign.runtime.router.httpx.post", boom)


def assert_no_secret_leak(router: Router) -> None:
    assert SECRET not in (router.last_error or "")
    assert SECRET not in json.dumps(router.snapshot())


def test_anthropic_success_parses_text_and_counts_real_usage(tmp_path, monkeypatch):
    monkeypatch.setenv(ANTHROPIC_REF, SECRET)
    cfg = live_config(tmp_path, provider="api", api_key_ref=ANTHROPIC_REF, api_timeout_s=12.5)
    calls = patch_post(monkeypatch, FakeResponse(200, ANTHROPIC_PAYLOAD))
    router = Router(cfg)

    text = router.complete("write me a plan", tier="work", system="be terse")

    assert text == "anthropic says hi"
    assert not router.degraded
    assert router.queued == 0
    assert router.usage.calls == 1
    assert router.usage.tokens == 123  # real input+output usage, not the char/4 estimate
    assert router.usage.by_tier == {"fast": 0, "work": 123, "think": 0}

    [call] = calls
    assert call["url"] == cfg.models.api_base_url
    assert call["timeout"] == 12.5
    assert call["headers"]["x-api-key"] == SECRET
    assert call["headers"]["anthropic-version"] == "2023-06-01"
    assert call["headers"]["content-type"] == "application/json"
    assert call["json"]["model"] == cfg.models.work
    assert call["json"]["system"] == "be terse"
    assert call["json"]["messages"] == [{"role": "user", "content": "write me a plan"}]
    assert call["json"]["max_tokens"] > 0
    assert_no_secret_leak(router)


def test_provider_api_names_itself_and_snapshot_has_no_secret(tmp_path, monkeypatch):
    monkeypatch.setenv(ANTHROPIC_REF, SECRET)
    cfg = live_config(tmp_path, provider="api", api_key_ref=ANTHROPIC_REF)
    router = Router(cfg)
    snap = router.snapshot()
    assert router.provider_name() == "api:anthropic"
    assert snap["provider"] == "api:anthropic"
    assert snap["api_configured"] is True
    assert SECRET not in json.dumps(snap)


def test_claude_unavailable_falls_back_to_api_when_allowed(tmp_path, monkeypatch):
    monkeypatch.setenv(ANTHROPIC_REF, SECRET)
    cfg = live_config(
        tmp_path, provider="claude_code", allow_api_fallback=True, api_key_ref=ANTHROPIC_REF
    )
    calls = patch_post(monkeypatch, FakeResponse(200, ANTHROPIC_PAYLOAD))
    router = Router(cfg)
    monkeypatch.setattr(router.claude, "available", lambda: False)

    text = router.complete("write me a plan", tier="work")

    assert text == "anthropic says hi"
    assert len(calls) == 1
    assert not router.degraded
    assert router.queued == 0
    assert router.usage.tokens == 123
    assert_no_secret_leak(router)


def test_claude_exception_falls_back_to_api_when_allowed(tmp_path, monkeypatch):
    monkeypatch.setenv(ANTHROPIC_REF, SECRET)
    cfg = live_config(
        tmp_path, provider="claude_code", allow_api_fallback=True, api_key_ref=ANTHROPIC_REF
    )
    calls = patch_post(monkeypatch, FakeResponse(200, ANTHROPIC_PAYLOAD))
    router = Router(cfg)
    monkeypatch.setattr(router.claude, "available", lambda: True)

    def exploding(*_args, **_kwargs):
        raise RuntimeError("cli exploded")

    monkeypatch.setattr(router.claude, "complete", exploding)
    text = router.complete("write me a plan", tier="fast")

    assert text == "anthropic says hi"
    assert len(calls) == 1
    assert not router.degraded
    assert router.usage.by_tier["fast"] == 123


def test_fallback_disabled_queues_and_never_calls_httpx(tmp_path, monkeypatch):
    monkeypatch.setenv(ANTHROPIC_REF, SECRET)
    cfg = live_config(
        tmp_path, provider="claude_code", allow_api_fallback=False, api_key_ref=ANTHROPIC_REF
    )
    forbid_post(monkeypatch)
    router = Router(cfg)
    monkeypatch.setattr(router.claude, "available", lambda: False)

    assert router.complete("write me a plan", tier="work") == ""
    assert router.degraded
    assert router.queued == 1
    assert router.last_error == "claude CLI unavailable; API fallback disabled"
    assert_no_secret_leak(router)


def test_fallback_allowed_without_key_queues_as_not_configured(tmp_path, monkeypatch):
    monkeypatch.delenv(ANTHROPIC_REF, raising=False)
    cfg = live_config(
        tmp_path, provider="claude_code", allow_api_fallback=True, api_key_ref=ANTHROPIC_REF
    )
    forbid_post(monkeypatch)
    router = Router(cfg)
    monkeypatch.setattr(router.claude, "available", lambda: False)

    assert router.complete("write me a plan", tier="work") == ""
    assert router.degraded
    assert "API fallback is not configured" in (router.last_error or "")


def test_openai_style_parses_choices_and_total_usage(tmp_path, monkeypatch):
    monkeypatch.setenv(OPENAI_REF, SECRET)
    cfg = live_config(
        tmp_path,
        provider="api",
        api_style="openai",
        api_base_url="https://api.openai.example/v1/chat/completions",
        api_key_ref=OPENAI_REF,
        fast="gpt-4o-mini",
        work="gpt-4o",
        think="o3",
    )
    calls = patch_post(monkeypatch, FakeResponse(200, OPENAI_PAYLOAD))
    router = Router(cfg)

    text = router.complete("write me a plan", tier="think", system="be terse")

    assert text == "openai says hi"
    assert router.provider_name() == "api:openai"
    assert router.usage.tokens == 77
    assert router.usage.by_tier["think"] == 77

    [call] = calls
    assert call["url"] == "https://api.openai.example/v1/chat/completions"
    assert call["headers"]["authorization"] == f"Bearer {SECRET}"
    assert call["json"]["model"] == "o3"
    assert call["json"]["messages"] == [
        {"role": "system", "content": "be terse"},
        {"role": "user", "content": "write me a plan"},
    ]
    assert_no_secret_leak(router)


def test_non_200_queues_degraded_without_leaking_key(tmp_path, monkeypatch):
    monkeypatch.setenv(ANTHROPIC_REF, SECRET)
    cfg = live_config(tmp_path, provider="api", api_key_ref=ANTHROPIC_REF)
    patch_post(monkeypatch, FakeResponse(500, {"error": "boom"}))
    router = Router(cfg)

    assert router.complete("write me a plan", tier="work") == ""
    assert router.degraded
    assert router.queued == 1
    assert "HTTP 500" in (router.last_error or "")
    assert router.usage.calls == 0
    assert_no_secret_leak(router)


def test_malformed_body_queues_without_leaking_key(tmp_path, monkeypatch):
    monkeypatch.setenv(ANTHROPIC_REF, SECRET)
    cfg = live_config(tmp_path, provider="api", api_key_ref=ANTHROPIC_REF)
    patch_post(monkeypatch, FakeResponse(200, {"unexpected": True}))
    router = Router(cfg)

    assert router.complete("write me a plan", tier="work") == ""
    assert router.degraded
    assert "malformed" in (router.last_error or "")
    assert_no_secret_leak(router)


def test_missing_usage_falls_back_to_estimated_tokens(tmp_path, monkeypatch):
    monkeypatch.setenv(ANTHROPIC_REF, SECRET)
    cfg = live_config(tmp_path, provider="api", api_key_ref=ANTHROPIC_REF)
    patch_post(monkeypatch, FakeResponse(200, {"content": [{"text": "no usage here"}]}))
    router = Router(cfg)

    prompt, system = "p" * 100, "be terse"
    text = router.complete(prompt, tier="work", system=system)

    assert text == "no usage here"
    est = max(32, (len(system) + len(prompt)) // 4 + 256)
    assert router.usage.tokens == est + len(text) // 4


def test_available_false_when_no_key_resolves(tmp_path, monkeypatch):
    monkeypatch.delenv(ANTHROPIC_REF, raising=False)
    cfg = live_config(tmp_path, provider="api", api_key_ref=ANTHROPIC_REF)
    forbid_post(monkeypatch)
    router = Router(cfg)

    assert not router.api.available()
    assert router.provider_name() == "unavailable"
    assert router.complete("write me a plan", tier="work") == ""
    assert router.degraded
    assert ANTHROPIC_REF in (router.last_error or "")


def test_provider_unavailable_raises_inside_api_provider(tmp_path, monkeypatch):
    monkeypatch.delenv(ANTHROPIC_REF, raising=False)
    cfg = live_config(tmp_path, api_key_ref=ANTHROPIC_REF)
    with pytest.raises(RuntimeError, match="no api key"):
        ApiProvider(cfg.models).complete("prompt", "fast", "system")


def test_sim_mode_uses_sim_brain_and_never_touches_httpx(tmp_path, monkeypatch):
    monkeypatch.setenv(ANTHROPIC_REF, SECRET)
    cfg = EngineConfig(mode="sim", data_dir=tmp_path)  # type: ignore[arg-type]
    cfg.models.provider = "api"
    cfg.models.api_key_ref = ANTHROPIC_REF
    forbid_post(monkeypatch)
    router = Router(cfg)

    text = router.complete("write a proposal please", tier="work")

    assert "USDC" in text
    assert router.provider_name() == "sim-brain"
    assert not router.degraded
    assert router.usage.calls == 1
    assert_no_secret_leak(router)


def test_secret_resolver_takes_precedence_over_environ(tmp_path, monkeypatch):
    monkeypatch.setenv(ANTHROPIC_REF, "env-key-should-lose")
    cfg = live_config(tmp_path, provider="api", api_key_ref=ANTHROPIC_REF)
    calls = patch_post(monkeypatch, FakeResponse(200, ANTHROPIC_PAYLOAD))
    seen_refs: list[str] = []

    def resolver(ref: str) -> str | None:
        seen_refs.append(ref)
        return SECRET

    router = Router(cfg, secret_resolver=resolver)
    assert router.api.available()
    assert router.complete("write me a plan", tier="work") == "anthropic says hi"
    assert ANTHROPIC_REF in seen_refs
    assert calls[0]["headers"]["x-api-key"] == SECRET
    assert_no_secret_leak(router)


def test_environ_is_fallback_when_resolver_misses_or_raises(tmp_path, monkeypatch):
    monkeypatch.setenv(ANTHROPIC_REF, SECRET)
    cfg = live_config(tmp_path, provider="api", api_key_ref=ANTHROPIC_REF)
    calls = patch_post(monkeypatch, FakeResponse(200, ANTHROPIC_PAYLOAD))

    router = Router(cfg, secret_resolver=lambda _ref: None)
    assert router.complete("write me a plan", tier="work") == "anthropic says hi"
    assert calls[0]["headers"]["x-api-key"] == SECRET

    def broken(_ref: str) -> str | None:
        raise RuntimeError("vault offline")

    assert Router(cfg, secret_resolver=broken).api.available()


def test_jailed_crafting_never_falls_back_to_api(tmp_path, monkeypatch):
    monkeypatch.setenv(ANTHROPIC_REF, SECRET)
    cfg = live_config(
        tmp_path, provider="claude_code", allow_api_fallback=True, api_key_ref=ANTHROPIC_REF
    )
    forbid_post(monkeypatch)
    router = Router(cfg)
    monkeypatch.setattr(router.claude, "available", lambda: False)
    work_root = tmp_path / "work"
    job_dir = work_root / "job_api0001"
    job_dir.mkdir(parents=True)

    assert router.complete_in_dir("craft it", job_dir, work_root) == ""
    assert router.degraded
    assert "cannot run jailed crafting" in (router.last_error or "")
    assert_no_secret_leak(router)


def test_jailed_crafting_queues_when_provider_is_api(tmp_path, monkeypatch):
    monkeypatch.setenv(ANTHROPIC_REF, SECRET)
    cfg = live_config(tmp_path, provider="api", api_key_ref=ANTHROPIC_REF)
    forbid_post(monkeypatch)
    router = Router(cfg)
    work_root = tmp_path / "work"
    job_dir = work_root / "job_api0002"
    job_dir.mkdir(parents=True)

    assert router.complete_in_dir("craft it", job_dir, work_root) == ""
    assert router.degraded
    assert "cannot run jailed crafting" in (router.last_error or "")
    assert_no_secret_leak(router)
