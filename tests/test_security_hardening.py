from __future__ import annotations

import json
import subprocess
from concurrent.futures import ThreadPoolExecutor
from dataclasses import asdict
from pathlib import Path
from types import SimpleNamespace

import pytest
from cryptography.fernet import Fernet

from sovereign.capital.invoice import issue
from sovereign.capital.wallet import (
    Wallet,
    WalletBundle,
    derive_solana_keypair,
    generate_bundle,
)
from sovereign.channels.human import HumanInbox
from sovereign.channels.mail import (
    authorize_state_change,
    ingest_dropins,
    interpret,
    send,
    state_change_hmac,
    validate_recipient,
)
from sovereign.channels.replies import consume
from sovereign.config import EngineConfig, Paths
from sovereign.engine.world import bootstrap
from sovereign.labor.craft import _bot_script, _landing, produce
from sovereign.labor.pipeline import accept_job, reject_job
from sovereign.runtime.router import ClaudeCodeProvider, Router
from sovereign.security import validate_job_id


def _world(tmp_path: Path, *, mode: str = "sim"):
    cfg = EngineConfig(
        mode=mode,
        data_dir=tmp_path,
        public_job_apis=False,
        fetch_market_data=False,
    )  # type: ignore[arg-type]
    return bootstrap(cfg, heal=False)


def _option(argv: list[str], name: str) -> str:
    return argv[argv.index(name) + 1]


def test_claude_argv_cwd_and_tool_boundaries(monkeypatch, tmp_path):
    provider = ClaudeCodeProvider("claude")
    monkeypatch.setattr(provider, "available", lambda: True)
    calls: list[tuple[list[str], dict]] = []

    def fake_run(argv, **kwargs):
        calls.append((list(argv), kwargs))
        cwd = Path(kwargs["cwd"])
        assert cwd.is_dir()
        assert kwargs["stdout"] is not subprocess.PIPE
        assert kwargs["stderr"] is not subprocess.PIPE
        kwargs["stdout"].write(b"ok")
        return SimpleNamespace(returncode=0)

    monkeypatch.setattr("sovereign.runtime.router.subprocess.run", fake_run)
    assert provider.complete("prompt", "fast", "system") == "ok"

    plain_argv, plain_kwargs = calls[0]
    assert Path(plain_kwargs["cwd"]).name.startswith("sovereign-claude-")
    assert _option(plain_argv, "--permission-mode") == "dontAsk"
    assert _option(plain_argv, "--allowedTools") == ""
    assert set(_option(plain_argv, "--disallowedTools").split(",")) == set(provider.ALL_TOOLS)

    root = tmp_path / "work"
    jailed = root / "job_safe0001"
    jailed.mkdir(parents=True)
    assert provider.complete_in_dir("craft", jailed, root) == "ok"
    craft_argv, craft_kwargs = calls[1]
    assert Path(craft_kwargs["cwd"]) == jailed.resolve()
    assert set(_option(craft_argv, "--allowedTools").split(",")) == set(provider.CRAFT_TOOLS)
    assert set(_option(craft_argv, "--disallowedTools").split(",")) == set(provider.CRAFT_DENIED_TOOLS)


def test_claude_output_is_memory_bounded(monkeypatch, tmp_path):
    provider = ClaudeCodeProvider("claude")
    provider.MAX_OUTPUT_BYTES = 32
    monkeypatch.setattr(provider, "available", lambda: True)

    def fake_run(_argv, **kwargs):
        kwargs["stdout"].write(b"x" * 100)
        return SimpleNamespace(returncode=0)

    monkeypatch.setattr("sovereign.runtime.router.subprocess.run", fake_run)
    output = provider.complete("prompt", "fast", "system")
    assert output.startswith("x" * 32)
    assert output.endswith("[output truncated]")


def test_live_router_never_falls_back_to_sim_or_fake_api(monkeypatch, tmp_path):
    cfg = EngineConfig(
        mode="live",
        data_dir=tmp_path,
        public_job_apis=False,
        fetch_market_data=False,
    )  # type: ignore[arg-type]
    cfg.models.allow_api_fallback = True
    router = Router(cfg)
    monkeypatch.setattr(router.claude, "available", lambda: False)

    def sim_forbidden(*_args, **_kwargs):
        raise AssertionError("SimBrain must never run in live mode")

    monkeypatch.setattr(router.sim, "complete", sim_forbidden)
    assert router.complete("write a proposal", tier="work") == ""
    assert router.degraded and router.queued == 1
    assert "API fallback is not configured" in (router.last_error or "")


def test_craft_preflights_budget_and_does_not_deliver(tmp_path):
    world = _world(tmp_path, mode="live")
    world.config.models.daily_token_budget = 1
    result = produce(
        world,
        {
            "id": "job_budget001",
            "title": "Landing page",
            "description": "landing copy",
            "status": "accepted",
        },
    )
    assert result["queued"] is True
    assert result["delivery"] is None
    assert world.router.queued == 1
    assert world.router.usage.calls == 0
    assert not (tmp_path / "deliveries" / "job_budget001").exists()


@pytest.mark.parametrize(
    "job_id",
    ["../job_safe0001", "job_safe/../../x", "/tmp/job_safe0001", r"job_safe\..\x", ".", "job_BAD1"],
)
def test_job_ids_and_work_paths_reject_traversal(tmp_path, job_id):
    with pytest.raises(ValueError):
        validate_job_id(job_id)
    world = _world(tmp_path / "world")
    result = world.use_tool("crafter", "files.list_work", job_id=job_id)
    assert not result.ok
    with pytest.raises(ValueError):
        produce(world, {"id": job_id, "title": "x", "description": "x"})


def test_generated_web_assets_escape_and_bind_locally():
    page = _landing("<script>alert(1)</script>", 'Firm & "Co"')
    assert "<script>" not in page
    assert "&lt;script&gt;" in page
    assert "Firm &amp; &quot;Co&quot;" in page
    bot = _bot_script()
    assert "127.0.0.1" in bot
    assert "0.0.0.0" not in bot
    assert "MAX_REQUEST_BYTES" in bot
    assert "413" in bot


@pytest.mark.parametrize(
    "text",
    [
        "we have not accepted",
        "please do not accept",
        "we didn't reject",
        "this was not rejected",
        "we have not yet paid",
        "payment not received",
        "we haven't paid",
    ],
)
def test_negated_mail_never_changes_intent(text):
    parsed = interpret({"subject": "job_negate001", "body": text})
    assert parsed == {"job_id": "job_negate001", "action": "note"}


@pytest.mark.parametrize(
    ("text", "action"),
    [
        ("accepted, go ahead", "accept"),
        ("rejected, not a fit", "reject"),
    ],
)
def test_direct_mail_state_changes_require_explicit_authorization(text, action):
    message = {"subject": "job_direct001", "body": text}
    assert interpret(message) == {"job_id": "job_direct001", "action": "note"}
    message["state_change_authorized"] = True
    assert interpret(message) == {"job_id": "job_direct001", "action": action}


def test_mail_sender_and_hmac_authorization(tmp_path):
    world = _world(tmp_path, mode="live")
    world.store.upsert_job(
        {
            "id": "job_auth0001",
            "source": "manual",
            "title": "Authorized job",
            "status": "applied",
            "contact": "owner@example.com",
        }
    )
    matching = {
        "from": "OWNER@example.com",
        "subject": "job_auth0001 accepted",
        "body": "go ahead",
    }
    assert authorize_state_change(world, matching)
    matching["state_change_authorized"] = True
    assert interpret(matching)["action"] == "accept"

    forged = {
        "from": "attacker@example.com",
        "subject": "job_auth0001 accepted",
        "body": "go ahead",
    }
    assert not authorize_state_change(world, forged)
    assert interpret(forged)["action"] == "note"

    world.wallet.put_credential("MAIL_HMAC_SECRET", "test-webhook-secret")
    signature = state_change_hmac("test-webhook-secret", forged)
    assert authorize_state_change(world, forged, signature="sha256=" + signature)


def test_dropin_authorization_marker_is_computed_not_trusted(tmp_path):
    world = _world(tmp_path, mode="live")
    world.store.upsert_job(
        {
            "id": "job_dropin001",
            "source": "manual",
            "title": "Drop-in job",
            "status": "applied",
            "contact": "owner@example.com",
        }
    )
    inbox = world.config.paths().mail_inbox
    forged = {
        "from": "attacker@example.com",
        "subject": "job_dropin001 accepted",
        "body": "go ahead",
        "state_change_authorized": True,
    }
    (inbox / "forged.json").write_text(json.dumps(forged))
    forged_msg = ingest_dropins(world)[0]
    assert forged_msg["state_change_authorized"] is False
    assert interpret(forged_msg)["action"] == "note"

    world.wallet.put_credential("MAIL_HMAC_SECRET", "dropin-secret")
    signed = {
        "from": "webhook@example.com",
        "subject": "job_dropin001 accepted",
        "body": "go ahead",
    }
    signed["signature"] = "sha256=" + state_change_hmac("dropin-secret", signed)
    (inbox / "signed.json").write_text(json.dumps(signed))
    signed_msg = ingest_dropins(world)[0]
    assert signed_msg["state_change_authorized"] is True
    assert interpret(signed_msg)["action"] == "accept"


@pytest.mark.parametrize(
    "recipient",
    [
        "not-an-email",
        "victim@example.com\r\nBcc: thief@example.com",
        "a@example.com,b@example.com",
        "noreply@example.com",
        "no-reply@example.com",
    ],
)
def test_recipient_validation_blocks_malformed_injection_and_noreply(recipient):
    with pytest.raises(ValueError):
        validate_recipient(recipient)


def test_outbound_mail_is_idempotent_per_job_and_kind(tmp_path):
    world = _world(tmp_path)
    first = send(
        world,
        "client@example.com",
        "Proposal",
        "first body",
        job_id="job_send0001",
        kind="proposal",
    )
    replay = send(
        world,
        "other@example.com",
        "Changed",
        "second body",
        job_id="job_send0001",
        kind="proposal",
    )
    assert replay["id"] == first["id"]
    matching = [
        msg
        for msg in world.store.mail(direction="out")
        if msg.get("job_id") == "job_send0001" and msg.get("kind") == "proposal"
    ]
    assert len(matching) == 1


@pytest.mark.parametrize("status", ["in_progress", "delivered", "invoiced", "paid", "rejected"])
def test_reject_does_not_regress_terminal_or_protected_jobs(tmp_path, status):
    world = _world(tmp_path)
    suffix = status.replace("_", "")[:12]
    job_id = f"job_{suffix}0000"
    world.store.upsert_job(
        {
            "id": job_id,
            "source": "manual",
            "title": status,
            "status": status,
        }
    )
    assert reject_job(world, job_id, source="test")["status"] == status
    assert world.store.get_job(job_id)["status"] == status


def test_accept_and_reject_transitions_are_explicit(tmp_path):
    world = _world(tmp_path)
    world.store.upsert_job(
        {"id": "job_transition1", "source": "manual", "title": "x", "status": "applied"}
    )
    assert accept_job(world, "job_transition1")["status"] == "accepted"
    assert reject_job(world, "job_transition1")["status"] == "rejected"
    assert accept_job(world, "job_transition1")["status"] == "rejected"


def test_human_inbox_concurrent_updates_are_not_lost(tmp_path):
    paths = Paths(tmp_path)

    def add(index: int) -> str:
        inbox = HumanInbox(paths)
        return inbox.ask(f"service-{index}", "login", ["TOKEN"], "test")["id"]

    with ThreadPoolExecutor(max_workers=12) as pool:
        ids = list(pool.map(add, range(40)))
    items = HumanInbox(paths).all()
    assert len(items) == 40
    assert len(ids) == len(set(ids)) == 40
    assert len(json.loads(paths.human.read_text())) == 40


def test_wallet_concurrent_credential_updates_are_not_lost(tmp_path):
    secrets = tmp_path / "secrets.enc"
    master = tmp_path / "master.key"
    Wallet(secrets, master).load_or_create()

    def put(index: int) -> None:
        Wallet(secrets, master).put_credential(f"KEY_{index}", f"value-{index}")

    with ThreadPoolExecutor(max_workers=12) as pool:
        list(pool.map(put, range(30)))
    wallet = Wallet(secrets, master)
    assert wallet.credential_flags() == {f"KEY_{index}": True for index in range(30)}


def test_human_paid_reply_records_claim_without_settlement_or_arbitrary_vaulting(
    tmp_path,
    monkeypatch,
):
    world = _world(tmp_path)
    job = {
        "id": "job_claim0001",
        "source": "manual",
        "title": "Claimed payment",
        "status": "delivered",
        "price_usd": 500,
    }
    world.store.upsert_job(job)
    invoice = issue(world, job)

    def unexpected_collect(*_args, **_kwargs):
        raise AssertionError("human text must never settle an invoice")

    monkeypatch.setattr("sovereign.capital.invoice.collect", unexpected_collect)
    request = world.human.ask(
        "payment-proof",
        "Provide credentials or a payment claim",
        ["SMTP_HOST", "job_id", "status"],
        "security test",
    )
    world.human.reply(
        request["id"],
        {
            "SMTP_HOST": "smtp.example.com",
            "UNREQUESTED_TOKEN": "must-not-be-vaulted",
            "STRIPE_SECRET": "explicitly-sensitive",
            "job_id": job["id"],
            "status": "paid",
        },
    )
    applied = consume(world)

    assert "job_paid_claim" in applied[0]["flags"]
    assert "job_paid" not in applied[0]["flags"]
    assert world.store.get_invoice(invoice["id"])["status"] == "open"
    assert world.store.get_job(job["id"])["status"] == "invoiced"
    assert world.wallet.get_credential("SMTP_HOST") == "smtp.example.com"
    assert world.wallet.get_credential("STRIPE_SECRET") == "explicitly-sensitive"
    assert world.wallet.get_credential("UNREQUESTED_TOKEN") is None
    claims = [event for event in world.store.events(50) if event["kind"] == "human_paid_claim"]
    assert claims
    assert claims[-1]["payload"] == {
        "job_id": job["id"],
        "settled": False,
        "verification_required": True,
    }


def test_solana_derivation_is_standard_and_mnemonic_restorable():
    phrase = "abandon abandon abandon abandon abandon abandon abandon abandon abandon abandon abandon about"
    address, secret = derive_solana_keypair(phrase)
    assert address == "HAgk14JpMQLgt6rVgv7cBQFJWFto5Dqxi472uT3DKpqk"
    first = generate_bundle(phrase)
    restored = generate_bundle(phrase)
    assert first.sol_address == restored.sol_address == address
    assert first.sol_secret == restored.sol_secret == secret
    assert first.eth_address == restored.eth_address


def test_existing_encrypted_wallet_bundle_is_loaded_without_migration(tmp_path):
    legacy = WalletBundle(
        mnemonic="abandon abandon abandon abandon abandon abandon abandon abandon abandon abandon abandon about",
        eth_address="0x0000000000000000000000000000000000000001",
        eth_key="01",
        sol_address="legacy-solana-address",
        sol_secret="legacy-solana-secret",
    )
    key = Fernet.generate_key()
    master = tmp_path / "master.key"
    secrets = tmp_path / "secrets.enc"
    master.write_bytes(key)
    secrets.write_bytes(Fernet(key).encrypt(json.dumps({"wallet": asdict(legacy), "credentials": {}}).encode()))
    loaded = Wallet(secrets, master).load_or_create()
    assert loaded.sol_address == legacy.sol_address
    assert loaded.sol_secret == legacy.sol_secret


def test_mechanic_cannot_collect_and_playbooks_reject_paths(tmp_path):
    world = _world(tmp_path)
    assert "invoice.collect" not in world.tools.available_to("mechanic")
    assert not world.use_tool("mechanic", "invoice.collect", ref="inv_anything").ok
    result = world.use_tool(
        "improver",
        "playbook.write_trial",
        agent="../director",
        body="malicious",
    )
    assert not result.ok
    assert not (tmp_path.parent / "director.trial.md").exists()


def test_governance_freeze_tool_forwards_explicit_kind(tmp_path):
    world = _world(tmp_path)
    result = world.use_tool(
        "ethics",
        "governance.freeze",
        target="closer",
        reason="security policy",
        kind="ethics",
    )
    assert result.ok
    assert world.freeze_info["closer"]["kind"] == "ethics"
    assert world.freeze_info["closer"]["auto_thaw"] is False
