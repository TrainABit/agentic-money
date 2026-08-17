from fastapi.testclient import TestClient

from sovereign.config import EngineConfig
from sovereign.dashboard.app import create_app
from sovereign.engine.heartbeat import step
from sovereign.engine.world import bootstrap


def test_full_sim_cycle_earns(tmp_path):
    cfg = EngineConfig(mode="sim", data_dir=tmp_path)  # type: ignore[arg-type]
    world = bootstrap(cfg)
    assert world.wallet.public()["eth_address"].startswith("0x")
    last = None
    for _ in range(18):
        last = step(world)
    assert last is not None
    snap = world.ledger.snapshot(now=world.now)
    paid = world.store.jobs("paid")
    assert len(paid) >= 1
    assert snap["revenue_usd"] >= 400
    assert snap["labor_usd"] >= 400
    assert snap["trailing_30d_usd"] >= 400
    status = world.status()
    assert status["goals"]["run_rate_usd"] == snap["trailing_30d_usd"]
    assert status["invoices_paid"] >= 1
    assert status["mail_out"] >= 1
    assert status["wallet"]["sol_address"]
    assert any(c.get("strategy_id") for c in world.certified)
    assert world.human.open()  # claude login request exists; work did not block on it
    kinds = {e["kind"] for e in world.store.events(250)}
    assert "audit" in kinds
    assert "direct" in kinds
    assert "treasury" in kinds
    assert "ethics" in kinds
    # Crafter wrote real files, not only a markdown blob
    crafted = [j for j in paid if j.get("files")]
    assert crafted


def test_dashboard_readonly(tmp_path):
    cfg = EngineConfig(mode="sim", data_dir=tmp_path)  # type: ignore[arg-type]
    bootstrap(cfg)
    client = TestClient(create_app(str(tmp_path), "sim"))
    r = client.get("/")
    assert r.status_code == 200
    s = client.get("/api/status")
    assert s.status_code == 200
    body = s.json()
    assert "goals" in body
    assert "wallet" in body
    assert "pipeline" in body
    j = client.get("/api/jobs")
    assert j.status_code == 200
