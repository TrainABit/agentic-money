from sovereign.config import EngineConfig
from sovereign.runtime.router import Router, SimBrain, jail_contains


def test_sim_brain_never_empty():
    b = SimBrain()
    assert "USDC" in b.complete("write a proposal please", "work", "sys")
    assert b.available()


def test_router_budget_degrades(tmp_path):
    cfg = EngineConfig(mode="sim", data_dir=tmp_path)  # type: ignore[arg-type]
    cfg.models.daily_token_budget = 10
    r = Router(cfg)
    text = r.complete("x" * 5000, tier="think")
    assert text
    assert r.usage.calls >= 1


def test_live_budget_queues_instead_of_canned_send(tmp_path):
    cfg = EngineConfig(mode="live", data_dir=tmp_path, public_job_apis=False, fetch_market_data=False)  # type: ignore[arg-type]
    cfg.models.daily_token_budget = 10
    r = Router(cfg)
    text = r.complete("x" * 5000, tier="work")
    assert text == ""
    assert r.degraded
    assert r.queued >= 1


def test_jail_rejects_prefix_sibling(tmp_path):
    root = tmp_path / "work"
    root.mkdir()
    evil = tmp_path / "work-evil"
    evil.mkdir()
    assert jail_contains(root / "job1", root)
    assert not jail_contains(evil, root)


def test_claude_jail_uses_allowlist_not_skip_permissions():
    import inspect

    from sovereign.runtime.router import ClaudeCodeProvider

    src = inspect.getsource(ClaudeCodeProvider.complete_in_dir)
    assert "--dangerously-skip-permissions" not in src
    assert "--permission-mode" in src
    assert "Bash" in src
