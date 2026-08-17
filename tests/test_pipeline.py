import json

from sovereign.capital.invoice import collect, issue, quote_usd
from sovereign.capital.payments import balance_of_calldata, decode_uint256
from sovereign.channels.mail import interpret, send
from sovereign.config import EngineConfig
from sovereign.engine.heartbeat import step
from sovereign.engine.world import bootstrap
from sovereign.labor.pipeline import accept_job


def _live(tmp_path) -> EngineConfig:
    return EngineConfig(
        mode="live",
        data_dir=tmp_path,
        public_job_apis=False,
        fetch_market_data=False,
    )  # type: ignore[arg-type]


def test_quote_and_calldata():
    assert quote_usd({"price_usd": 800}) == 800
    assert quote_usd({"fit": 1.0, "price_usd": 0}) > 1000
    data = balance_of_calldata("0x38F241B24841e9C134fF0a43042E1E99E6BB40E2")
    assert data.startswith("0x70a08231")
    assert len(data) == 2 + 8 + 64
    assert decode_uint256("0x1e240") == 123456


def test_live_apply_accept_invoice_pay(tmp_path):
    cfg = _live(tmp_path)
    world = bootstrap(cfg)
    world.store.upsert_job(
        {
            "id": "job_livepipe01",
            "source": "manual",
            "title": "Python CSV cleaner",
            "description": "python csv data automation",
            "status": "open",
            "price_usd": 500,
            "fit": 0.9,
            "contact": "ada@example.com",
        }
    )
    step(world)
    job = world.store.get_job("job_livepipe01")
    assert job["status"] == "applied"
    assert world.store.mail(direction="out")
    accept_job(world, "job_livepipe01", source="test")
    step(world)
    job = world.store.get_job("job_livepipe01")
    assert job["status"] in {"delivered", "invoiced"}
    inv = world.store.invoice_for_job("job_livepipe01")
    assert inv and inv["status"] == "open"
    assert inv["eth_address"].startswith("0x")
    before = world.ledger.balance("assets.usdc")
    collect(world, inv["id"], source="test")
    job = world.store.get_job("job_livepipe01")
    assert job["status"] == "paid"
    assert world.ledger.balance("assets.usdc") == before + 500
    assert (tmp_path / "work" / "job_livepipe01" / "README.md").exists()


def test_mail_dropin_accepts_job(tmp_path):
    cfg = EngineConfig(mode="sim", data_dir=tmp_path)  # type: ignore[arg-type]
    cfg.sim.auto_accept = False
    world = bootstrap(cfg)
    world.store.upsert_job(
        {
            "id": "job_mailaccept1",
            "source": "sim-market",
            "title": "Python bot",
            "description": "python bot automation",
            "status": "applied",
            "price_usd": 400,
            "fit": 0.8,
        }
    )
    drop = cfg.paths().mail_inbox / "lead.json"
    drop.write_text(
        json.dumps(
            {
                "from": "client@x.com",
                "subject": "Re: job_mailaccept1 ACCEPTED",
                "body": "go ahead",
            }
        )
    )
    step(world)
    assert world.store.get_job("job_mailaccept1")["status"] in {"accepted", "in_progress", "delivered", "invoiced", "paid"}


def test_human_reply_stores_credential_not_in_events(tmp_path):
    cfg = EngineConfig(mode="sim", data_dir=tmp_path)  # type: ignore[arg-type]
    world = bootstrap(cfg)
    req = world.human.ask("vps", "token", ["HETZNER_API_TOKEN"], "compute")
    world.human.reply(req["id"], {"HETZNER_API_TOKEN": "secret-token-value"})
    from sovereign.channels.replies import consume

    consume(world)
    assert world.wallet.get_credential("HETZNER_API_TOKEN") == "secret-token-value"
    blob = json.dumps(world.store.events(50))
    assert "secret-token-value" not in blob


def test_interpret_mail():
    assert interpret({"subject": "job_abc12345 accepted", "body": "go ahead"})["action"] == "accept"
    assert interpret({"subject": "paid txid 0x1", "body": "", "job_id": "job_abc12345"})["action"] == "paid"
