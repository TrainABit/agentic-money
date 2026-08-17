from sovereign.config import EngineConfig
from sovereign.engine.heartbeat import step
from sovereign.engine.world import bootstrap
from sovereign.runtime.router import Router, SimBrain


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
