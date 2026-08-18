"""AgentMail transport: polling, outbound selection, remote ingestion, courier.

No network and no real SDK anywhere: a fake client mirrors the exact SDK
method shapes (client.inboxes.messages.list/get/update/send) and fake
transports are injected through the ``world.agentmail_transport`` attribute
honored by the transport factory.
"""

from __future__ import annotations

import json
import sys
from datetime import timedelta
from types import SimpleNamespace
from typing import Any

import pytest

from sovereign.agents.roles import courier
from sovereign.channels import mail
from sovereign.channels.transports import (
    PROCESSED_LABEL,
    AgentMailTransport,
    resolve_outbound_transport,
)
from sovereign.config import EngineConfig
from sovereign.engine.heartbeat import step
from sovereign.engine.world import bootstrap


def _world(tmp_path, *, mode: str = "live"):
    cfg = EngineConfig(
        mode=mode,
        data_dir=tmp_path,
        public_job_apis=False,
        fetch_market_data=False,
    )  # type: ignore[arg-type]
    return bootstrap(cfg, heal=False)


def _vault_agentmail(world) -> None:
    world.wallet.put_credential("AGENTMAIL_API_KEY", "am-test-key")
    world.wallet.put_credential("AGENTMAIL_INBOX_ID", "inb_test")


class FakeMessagesApi:
    """Mimics client.inboxes.messages with the exact SDK method shapes."""

    def __init__(self, pages=None, full=None):
        self.pages = list(pages or [])
        self.full = dict(full or {})
        self.list_calls: list[dict[str, Any]] = []
        self.get_calls: list[str] = []
        self.update_calls: list[dict[str, Any]] = []
        self.sent: list[dict[str, Any]] = []
        self.fail_get: set[str] = set()
        self.fail_update: set[str] = set()
        self.send_error: Exception | None = None

    def list(self, *, inbox_id: str, limit=None, page_token=None):
        self.list_calls.append(
            {"inbox_id": inbox_id, "limit": limit, "page_token": page_token}
        )
        index = 0 if page_token is None else int(page_token)
        if index >= len(self.pages):
            return SimpleNamespace(messages=[], next_page_token=None)
        return self.pages[index]

    def get(self, *, inbox_id: str, message_id: str):
        self.get_calls.append(message_id)
        if message_id in self.fail_get:
            raise RuntimeError(f"get failed for {message_id}")
        return self.full[message_id]

    def update(self, *, inbox_id: str, message_id: str, add_labels=None, remove_labels=None):
        if message_id in self.fail_update:
            raise RuntimeError(f"update failed for {message_id}")
        self.update_calls.append(
            {
                "message_id": message_id,
                "add_labels": list(add_labels or []),
                "remove_labels": list(remove_labels or []),
            }
        )

    def send(self, *, inbox_id: str, to, subject, text):
        if self.send_error is not None:
            raise self.send_error
        self.sent.append({"inbox_id": inbox_id, "to": to, "subject": subject, "text": text})


def _client(api: FakeMessagesApi):
    return SimpleNamespace(inboxes=SimpleNamespace(messages=api))


def _transport(api: FakeMessagesApi, inbox_id: str = "inb_test") -> AgentMailTransport:
    return AgentMailTransport(api_key="am-test-key", inbox_id=inbox_id, client=_client(api))


def _meta(message_id: str, labels=("received",)):
    return SimpleNamespace(message_id=message_id, labels=list(labels))


def _full(message_id: str, **overrides):
    base = {
        "message_id": message_id,
        "from_": "sender@example.com",
        "subject": "Subject line",
        "extracted_text": None,
        "text": None,
        "extracted_html": None,
        "html": None,
        "timestamp": "2026-08-18T00:00:00+00:00",
        "labels": ["received"],
    }
    base.update(overrides)
    return SimpleNamespace(**base)


class FakeTransport:
    """Injected via world.agentmail_transport for courier/pipeline tests."""

    def __init__(self, items=None):
        self.items = list(items or [])
        self.poll_calls: list[dict[str, Any]] = []

    def poll(self, limit: int = 25, seen_ids=None):
        self.poll_calls.append({"limit": limit, "seen_ids": set(seen_ids or set())})
        return list(self.items)

    def send(self, to: str, subject: str, body: str) -> None:
        raise AssertionError("outbound send not expected in this test")


# ---------------------------------------------------------------- poll


def test_poll_pages_skips_seen_and_labeled_and_labels_back():
    api = FakeMessagesApi(
        pages=[
            SimpleNamespace(
                messages=[
                    _meta("m1"),
                    _meta("m2", labels=["received", PROCESSED_LABEL]),
                    _meta("m3"),
                ],
                next_page_token="1",
            ),
            SimpleNamespace(messages=[_meta("m4")], next_page_token=None),
        ],
        full={
            "m1": _full("m1", extracted_text="clean reply", text="quoted > history"),
            "m3": _full("m3", html="<p>html body</p>"),
            "m4": _full("m4", text="plain"),
        },
    )
    transport = _transport(api)
    items = transport.poll(limit=2, seen_ids={"m4"})

    assert [i["agentmail_id"] for i in items] == ["m1", "m3"]
    assert items[0]["body"] == "clean reply"  # extracted_text beats text
    assert items[1]["body"] == "<p>html body</p>"  # html as last resort
    assert items[0]["from"] == "sender@example.com"
    assert items[0]["subject"] == "Subject line"
    assert items[0]["ts"] == "2026-08-18T00:00:00+00:00"
    # pagination follows next_page_token with the configured inbox and limit
    assert [c["page_token"] for c in api.list_calls] == [None, "1"]
    assert all(c["inbox_id"] == "inb_test" and c["limit"] == 2 for c in api.list_calls)
    # m2 (already labeled) and m4 (seen id) are never fetched
    assert api.get_calls == ["m1", "m3"]
    # processed label written back for every returned message
    assert [u["message_id"] for u in api.update_calls] == ["m1", "m3"]
    assert all(u["add_labels"] == [PROCESSED_LABEL] for u in api.update_calls)


@pytest.mark.parametrize(
    ("fields", "expected"),
    [
        ({"extracted_text": "et", "text": "t", "extracted_html": "eh", "html": "h"}, "et"),
        ({"text": "t", "extracted_html": "eh", "html": "h"}, "t"),
        ({"extracted_html": "eh", "html": "h"}, "eh"),
        ({"html": "h"}, "h"),
        ({}, ""),
    ],
)
def test_poll_body_precedence(fields, expected):
    api = FakeMessagesApi(
        pages=[SimpleNamespace(messages=[_meta("m1")], next_page_token=None)],
        full={"m1": _full("m1", **fields)},
    )
    [item] = _transport(api).poll(seen_ids=set())
    assert item["body"] == expected


def test_poll_caps_body_length():
    api = FakeMessagesApi(
        pages=[SimpleNamespace(messages=[_meta("m1")], next_page_token=None)],
        full={"m1": _full("m1", text="x" * 30_000)},
    )
    [item] = _transport(api).poll(seen_ids=set())
    assert len(item["body"]) == 20_000


def test_poll_isolates_per_message_failures():
    api = FakeMessagesApi(
        pages=[
            SimpleNamespace(
                messages=[_meta("m1"), _meta("m2"), _meta("m3")], next_page_token=None
            )
        ],
        full={
            "m1": _full("m1", text="one"),
            "m2": _full("m2", text="two"),
            "m3": _full("m3", text="three"),
        },
    )
    api.fail_get = {"m1"}
    api.fail_update = {"m2"}
    items = _transport(api).poll(seen_ids=set())
    assert [i["agentmail_id"] for i in items] == ["m3"]
    assert [u["message_id"] for u in api.update_calls] == ["m3"]


def test_poll_hard_caps_total_scanned_messages():
    pages = [
        SimpleNamespace(messages=[_meta(f"m{i}") for i in range(60)], next_page_token="1"),
        SimpleNamespace(
            messages=[_meta(f"m{i}") for i in range(60, 120)], next_page_token=None
        ),
    ]
    full = {f"m{i}": _full(f"m{i}", text="body") for i in range(120)}
    api = FakeMessagesApi(pages=pages, full=full)
    items = _transport(api).poll(limit=60, seen_ids=set())
    assert len(items) == 100
    assert len(api.get_calls) == 100


def test_missing_sdk_raises_clear_runtime_error(monkeypatch):
    monkeypatch.setitem(sys.modules, "agentmail", None)  # force ImportError
    with pytest.raises(RuntimeError) as err:
        AgentMailTransport(api_key="k", inbox_id="inb")
    assert "agentmail" in str(err.value)
    assert "[mail]" in str(err.value)


# ---------------------------------------------------- outbound selection


def test_send_prefers_agentmail_and_never_touches_smtp(tmp_path, monkeypatch):
    world = _world(tmp_path)
    _vault_agentmail(world)
    world.wallet.put_credential("SMTP_HOST", "smtp.example.com")
    api = FakeMessagesApi()
    world.agentmail_transport = _transport(api)

    def forbidden(*_args, **_kwargs):
        raise AssertionError("SMTP must not run when AgentMail is configured")

    monkeypatch.setattr(mail, "_smtp_send", forbidden)
    name, fn = resolve_outbound_transport(world)
    assert name == "agentmail" and callable(fn)

    msg = mail.send(
        world, "client@example.com", "Proposal", "body text",
        job_id="job_amsend0001", kind="proposal",
    )
    assert msg["status"] == "sent"
    assert msg["transport"] == "agentmail"
    assert api.sent == [
        {
            "inbox_id": "inb_test",
            "to": "client@example.com",
            "subject": "Proposal",
            "text": "body text",
        }
    ]
    paths = world.config.paths()
    assert not (paths.mail_outbox / f"{msg['id']}.json").exists()
    assert (paths.mail_sent / f"{msg['id']}.json").exists()


def test_send_agentmail_failure_queues_and_keeps_outbox_file(tmp_path):
    world = _world(tmp_path)
    _vault_agentmail(world)
    api = FakeMessagesApi()
    api.send_error = RuntimeError("provider down")
    world.agentmail_transport = _transport(api)

    msg = mail.send(world, "client@example.com", "Proposal", "body")
    assert msg["status"] == "queued"
    assert "provider down" in msg["send_error"]
    assert "transport" not in msg
    outbox_file = world.config.paths().mail_outbox / f"{msg['id']}.json"
    assert outbox_file.exists()
    assert json.loads(outbox_file.read_text())["id"] == msg["id"]


def test_send_queues_when_sdk_is_missing(tmp_path, monkeypatch):
    world = _world(tmp_path)
    _vault_agentmail(world)  # creds present but no injected transport and no SDK
    monkeypatch.setitem(sys.modules, "agentmail", None)
    msg = mail.send(world, "client@example.com", "Proposal", "body")
    assert msg["status"] == "queued"
    assert "agentmail" in msg["send_error"]


def test_send_uses_smtp_when_only_smtp_credentials_exist(tmp_path, monkeypatch):
    world = _world(tmp_path)
    world.wallet.put_credential("SMTP_HOST", "smtp.example.com")
    assert resolve_outbound_transport(world) == ("smtp", None)
    delivered: list[str] = []
    monkeypatch.setattr(mail, "_smtp_send", lambda _world, msg: delivered.append(msg["id"]))

    msg = mail.send(world, "client@example.com", "Proposal", "body")
    assert msg["status"] == "sent"
    assert delivered == [msg["id"]]
    assert "transport" not in msg
    assert "send_error" not in msg


def test_send_falls_back_to_local_outbox_without_credentials(tmp_path):
    world = _world(tmp_path)
    assert resolve_outbound_transport(world) == ("local", None)
    msg = mail.send(world, "client@example.com", "Proposal", "body")
    assert msg["status"] == "sent_local"
    assert (world.config.paths().mail_sent / f"{msg['id']}.json").exists()


# ------------------------------------------------- remote inbound ingest


def _item(agentmail_id: str, sender: str = "client@example.com", **overrides):
    item = {
        "agentmail_id": agentmail_id,
        "from": sender,
        "subject": "Hello",
        "body": "Just a note",
        "ts": "2026-08-18T00:00:00+00:00",
    }
    item.update(overrides)
    return item


def test_ingest_remote_inbound_writes_dropin_once(tmp_path):
    world = _world(tmp_path)
    items = [_item("msg_100")]
    assert mail.ingest_remote_inbound(world, items) == 1
    dropin = world.config.paths().mail_inbox / "am_msg_100.json"
    assert json.loads(dropin.read_text()) == {
        "from": "client@example.com",
        "subject": "Hello",
        "body": "Just a note",
        "ts": "2026-08-18T00:00:00+00:00",
        "source": "agentmail",
    }
    # second call with the same id is a no-op
    assert mail.ingest_remote_inbound(world, items) == 0
    assert world.store.get_kv("agentmail_seen_ids") == ["msg_100"]
    assert len(list(world.config.paths().mail_inbox.glob("am_*.json"))) == 1


def test_ingest_remote_inbound_caps_seen_ids_fifo(tmp_path):
    world = _world(tmp_path)
    world.store.set_kv("agentmail_seen_ids", [f"old_{i}" for i in range(500)])
    assert mail.ingest_remote_inbound(world, [_item("old_25")]) == 0  # tracked id skipped
    assert mail.ingest_remote_inbound(world, [_item("msg_new")]) == 1
    seen = world.store.get_kv("agentmail_seen_ids")
    assert len(seen) == 500
    assert seen[0] == "old_1"  # oldest evicted first
    assert "old_0" not in seen
    assert seen[-1] == "msg_new"


def test_unauthorized_remote_sender_cannot_change_job_state(tmp_path, monkeypatch):
    world = _world(tmp_path)
    monkeypatch.setattr(world.router, "complete", lambda *_a, **_k: "text")
    monkeypatch.setattr(world.router, "complete_in_dir", lambda *_a, **_k: "done")
    world.store.upsert_job(
        {
            "id": "job_amauth0001",
            "source": "manual",
            "title": "Guarded job",
            "status": "applied",
            "contact": "owner@example.com",
        }
    )
    mail.ingest_remote_inbound(
        world,
        [
            _item(
                "msg_evil",
                sender="attacker@example.com",
                subject="job_amauth0001 accepted",
                body="go ahead",
            )
        ],
    )
    step(world)  # heartbeat ingest_dropins runs authorize_state_change
    assert world.store.get_job("job_amauth0001")["status"] == "applied"
    inbound = [
        m
        for m in world.store.mail(direction="in")
        if m.get("address") == "attacker@example.com"
    ]
    assert inbound and inbound[0]["state_change_authorized"] is False


def test_authorized_agentmail_inbound_accepts_job_end_to_end(tmp_path, monkeypatch):
    world = _world(tmp_path)
    monkeypatch.setattr(world.router, "complete", lambda *_a, **_k: "proposal text")
    monkeypatch.setattr(world.router, "complete_in_dir", lambda *_a, **_k: "delivery complete")
    world.store.upsert_job(
        {
            "id": "job_amflow0001",
            "source": "manual",
            "title": "Python automation",
            "description": "python data automation",
            "status": "applied",
            "price_usd": 400,
            "fit": 0.9,
            "contact": "owner@example.com",
        }
    )
    _vault_agentmail(world)
    fake = FakeTransport(
        items=[
            _item(
                "msg_ok",
                sender="owner@example.com",
                subject="Re: job_amflow0001 accepted",
                body="go ahead",
            )
        ]
    )
    world.agentmail_transport = fake

    step(world)  # courier polls the fake inbox and writes the drop-in
    assert fake.poll_calls
    assert (world.config.paths().mail_inbox / "am_msg_ok.json").exists()

    step(world)  # ingest_dropins consumes it; contact sender is authorized
    status = world.store.get_job("job_amflow0001")["status"]
    assert status in {"accepted", "in_progress", "delivered", "invoiced", "paid"}
    inbound = [
        m
        for m in world.store.mail(direction="in")
        if m.get("address") == "owner@example.com"
    ]
    assert inbound and inbound[0]["state_change_authorized"] is True


# ----------------------------------------------------------- courier


def test_courier_polls_on_cadence_and_reports_counts(tmp_path):
    world = _world(tmp_path)
    _vault_agentmail(world)
    fake = FakeTransport(items=[_item("msg_c1")])
    world.agentmail_transport = fake

    [action] = courier(world)
    assert action["agentmail"] == {"polled": 1, "ingested": 1}
    assert len(fake.poll_calls) == 1
    assert fake.poll_calls[0]["limit"] == 25
    assert fake.poll_calls[0]["seen_ids"] == set()

    [again] = courier(world)  # same instant: cadence not due
    assert "agentmail" not in again
    assert len(fake.poll_calls) == 1

    world.now = world.now + timedelta(
        minutes=world.config.live_timing.mail_poll_minutes + 1
    )
    [later] = courier(world)
    assert len(fake.poll_calls) == 2
    assert fake.poll_calls[1]["seen_ids"] == {"msg_c1"}  # kv seen set is passed through
    assert later["agentmail"] == {"polled": 1, "ingested": 0}


def test_courier_never_polls_in_sim(tmp_path):
    world = _world(tmp_path, mode="sim")
    _vault_agentmail(world)
    fake = FakeTransport(items=[_item("msg_sim")])
    world.agentmail_transport = fake
    [action] = courier(world)
    assert fake.poll_calls == []
    assert "agentmail" not in action
    assert list(world.config.paths().mail_inbox.glob("am_*.json")) == []


def test_courier_asks_for_agentmail_only_when_no_mail_channel_exists(tmp_path):
    world = _world(tmp_path)
    courier(world)
    open_requests = {i["service"]: i for i in world.human.open()}
    assert "smtp" in open_requests
    assert open_requests["agentmail"]["fields"] == ["AGENTMAIL_API_KEY", "AGENTMAIL_INBOX_ID"]

    smtp_world = _world(tmp_path / "smtp_only")
    smtp_world.wallet.put_credential("SMTP_HOST", "smtp.example.com")
    courier(smtp_world)
    assert {i["service"] for i in smtp_world.human.open()} & {"smtp", "agentmail"} == set()

    am_world = _world(tmp_path / "agentmail_only")
    _vault_agentmail(am_world)
    am_world.agentmail_transport = FakeTransport()
    [action] = courier(am_world)
    services = {i["service"] for i in am_world.human.open()}
    assert "agentmail" not in services
    assert "smtp" in services  # existing SMTP nudge is preserved
    assert action["agentmail"] == {"polled": 0, "ingested": 0}
