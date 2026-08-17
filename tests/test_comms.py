from __future__ import annotations

import json
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta, timezone
from typing import Any

import pytest

from sovereign.comms import Bus, Message, SendReceipt
from sovereign.memory.store import Store

ROSTER = frozenset({"a", "b", "c", "d"})
BASE = datetime(2026, 1, 1, 12, 0, 0, tzinfo=timezone.utc)


def make_bus(tmp_path) -> tuple[Store, Bus]:
    store = Store(tmp_path / "comms.db")
    return store, Bus(store, ROSTER)


def events_of(store: Store, kind: str) -> list[dict[str, Any]]:
    return [e for e in store.events(limit=1000) if e["kind"] == kind]


def test_bus_requires_roster(tmp_path):
    store = Store(tmp_path / "comms.db")
    with pytest.raises(ValueError, match="roster"):
        Bus(store, frozenset())


def test_send_inbox_ack_roundtrip(tmp_path):
    store, bus = make_bus(tmp_path)
    receipt = bus.send("a", "b", "task.assign", {"step": 1}, now=BASE)
    assert isinstance(receipt, SendReceipt)
    assert receipt.thread_id.startswith("th_")
    assert receipt.correlation_id.startswith("co_")
    assert len(receipt.message_ids) == 1
    assert receipt.message_ids[0].startswith("msg_")

    assert bus.inbox("a", now=BASE) == []
    box = bus.inbox("b", now=BASE)
    assert len(box) == 1
    msg = box[0]
    assert msg.id == receipt.message_ids[0]
    assert msg.ts == BASE
    assert msg.thread_id == receipt.thread_id
    assert msg.correlation_id == receipt.correlation_id
    assert msg.sender == "a"
    assert msg.recipient == "b"
    assert msg.kind == "task.assign"
    assert msg.payload == {"step": 1}
    assert msg.status == "queued"
    assert msg.expects_reply is False
    assert msg.reply_to is None
    assert msg.deadline is None
    assert msg.attempts == 0
    assert msg.max_attempts == 3

    done = bus.ack(msg.id, now=BASE + timedelta(minutes=1))
    assert done.status == "done"
    assert bus.inbox("b", now=BASE + timedelta(minutes=1)) == []
    assert bus.counts() == {"done": 1}
    row = store.get_message(msg.id)
    assert row["status"] == "done"
    assert row["payload"] == {"step": 1}


def test_message_record_roundtrip(tmp_path):
    store, bus = make_bus(tmp_path)
    deadline = BASE + timedelta(hours=2)
    bus.request("a", "b", "quote.request", {"item": "vps"}, now=BASE, deadline=deadline)
    msg = bus.inbox("b", now=BASE)[0]
    record = msg.to_record()
    assert record["ts"] == BASE.isoformat()
    assert record["deadline"] == deadline.isoformat()
    assert record["expects_reply"] == 1
    assert Message.from_record(record) == msg
    assert store.get_message(msg.id)["deadline"] == deadline.isoformat()


def test_inbox_orders_oldest_first(tmp_path):
    store, bus = make_bus(tmp_path)
    second = bus.send("a", "b", "step.two", {}, now=BASE + timedelta(seconds=2))
    first = bus.send("c", "b", "step.one", {}, now=BASE)
    third = bus.send("d", "b", "step.three", {}, now=BASE + timedelta(seconds=4))
    box = bus.inbox("b", now=BASE + timedelta(minutes=1))
    assert [m.id for m in box] == [
        first.message_ids[0],
        second.message_ids[0],
        third.message_ids[0],
    ]


def test_multicast_shares_correlation_and_isolates_recipients(tmp_path):
    store, bus = make_bus(tmp_path)
    receipt = bus.send("a", ["b", "c", "b", "d"], "plan.update", {"rev": 7}, now=BASE)
    # Duplicate recipient deduped, order preserved.
    assert len(receipt.message_ids) == 3
    assert [store.get_message(mid)["recipient"] for mid in receipt.message_ids] == ["b", "c", "d"]

    rows = store.messages(correlation_id=receipt.correlation_id)
    assert {r["recipient"] for r in rows} == {"b", "c", "d"}
    assert {r["thread_id"] for r in rows} == {receipt.thread_id}
    assert len(store.messages(thread_id=receipt.thread_id)) == 3

    for agent in ("b", "c", "d"):
        box = bus.inbox(agent, now=BASE)
        assert len(box) == 1
        assert box[0].recipient == agent
        assert box[0].correlation_id == receipt.correlation_id
    assert bus.inbox("a", now=BASE) == []
    assert bus.counts() == {"queued": 3}


def test_broadcast_excludes_sender(tmp_path):
    store, bus = make_bus(tmp_path)
    receipt = bus.broadcast("c", "status.report", {"ok": True}, now=BASE)
    assert len(receipt.message_ids) == 3
    recipients = {store.get_message(mid)["recipient"] for mid in receipt.message_ids}
    assert recipients == {"a", "b", "d"}
    assert bus.inbox("c", now=BASE) == []
    for agent in ("a", "b", "d"):
        assert len(bus.inbox(agent, now=BASE)) == 1


def test_request_reply_roundtrip(tmp_path):
    store, bus = make_bus(tmp_path)
    deadline = BASE + timedelta(hours=1)
    receipt = bus.request("a", "b", "quote.request", {"sku": "gpu"}, now=BASE, deadline=deadline)
    co = receipt.correlation_id
    assert bus.outstanding(co) == 1
    assert bus.replies(co) == []

    request = bus.inbox("b", now=BASE)[0]
    assert request.expects_reply is True
    assert request.deadline == deadline

    reply_receipt = bus.reply(request, "b", {"price": 42}, now=BASE + timedelta(minutes=5))
    assert reply_receipt.thread_id == receipt.thread_id
    assert reply_receipt.correlation_id == co
    # Replying does not settle the request row; the recipient still acks it.
    assert bus.outstanding(co) == 1
    assert len(bus.replies(co)) == 1

    bus.ack(request.id, now=BASE + timedelta(minutes=5))
    assert bus.outstanding(co) == 0

    replies = bus.replies(co)
    assert len(replies) == 1
    reply = replies[0]
    assert reply.reply_to == request.id
    assert reply.kind == "quote.request.reply"
    assert reply.expects_reply is False
    assert reply.sender == "b"
    assert reply.recipient == "a"
    assert reply.thread_id == receipt.thread_id

    box_a = bus.inbox("a", now=BASE + timedelta(minutes=6))
    assert [m.id for m in box_a] == [reply.id]
    assert box_a[0].payload == {"price": 42}


def test_reply_to_reply_rejected(tmp_path):
    store, bus = make_bus(tmp_path)
    bus.request("a", "b", "quote.request", {}, now=BASE, deadline=BASE + timedelta(hours=1))
    request = bus.inbox("b", now=BASE)[0]
    bus.reply(request, "b", {"price": 9}, now=BASE + timedelta(minutes=1))
    reply_msg = bus.inbox("a", now=BASE + timedelta(minutes=2))[0]
    with pytest.raises(ValueError, match="reply to a reply"):
        bus.reply(reply_msg, "a", {"again": True}, now=BASE + timedelta(minutes=3))


def test_reply_from_wrong_sender_rejected(tmp_path):
    store, bus = make_bus(tmp_path)
    bus.request("a", "b", "quote.request", {}, now=BASE, deadline=BASE + timedelta(hours=1))
    request = bus.inbox("b", now=BASE)[0]
    with pytest.raises(ValueError, match="original recipient"):
        bus.reply(request, "c", {}, now=BASE + timedelta(minutes=1))
    with pytest.raises(ValueError, match="original recipient"):
        bus.reply(request, "a", {}, now=BASE + timedelta(minutes=1))


def test_reply_kind_default_and_truncation(tmp_path):
    store, bus = make_bus(tmp_path)
    long_kind = "k" * 64
    bus.send("a", "b", long_kind, {}, now=BASE)
    original = bus.inbox("b", now=BASE)[0]
    receipt = bus.reply(original, "b", {}, now=BASE + timedelta(seconds=1))
    reply_kind = store.get_message(receipt.message_ids[0])["kind"]
    assert reply_kind == "k" * 58 + ".reply"
    assert len(reply_kind) == 64

    bus.send("a", "b", "ping", {}, now=BASE + timedelta(seconds=2))
    plain = [m for m in bus.inbox("b", now=BASE + timedelta(seconds=3)) if m.kind == "ping"][0]
    custom = bus.reply(plain, "b", {}, now=BASE + timedelta(seconds=4), kind="pong")
    assert store.get_message(custom.message_ids[0])["kind"] == "pong"


def test_send_rejects_bad_arguments(tmp_path):
    store, bus = make_bus(tmp_path)
    ok = {"n": 1}
    with pytest.raises(ValueError, match="sender"):
        bus.send("zz", "b", "ping", ok, now=BASE)
    with pytest.raises(ValueError, match="recipient"):
        bus.send("a", "zz", "ping", ok, now=BASE)
    with pytest.raises(ValueError, match="recipient"):
        bus.send("a", ["b", "zz"], "ping", ok, now=BASE)
    with pytest.raises(ValueError, match="recipient"):
        bus.send("a", [], "ping", ok, now=BASE)
    for bad_kind in ("", "Nope", "1abc", "has space", "k" * 65, "dash-bad"):
        with pytest.raises(ValueError, match="kind"):
            bus.send("a", "b", bad_kind, ok, now=BASE)
    with pytest.raises(ValueError, match="payload"):
        bus.send("a", "b", "ping", ["not", "a", "dict"], now=BASE)
    with pytest.raises(ValueError, match="payload"):
        bus.send("a", "b", "ping", {"x": object()}, now=BASE)
    with pytest.raises(ValueError, match="payload"):
        bus.send("a", "b", "ping", {"blob": "x" * 40_000}, now=BASE)
    with pytest.raises(ValueError, match="now"):
        bus.send("a", "b", "ping", ok, now=datetime(2026, 1, 1))
    with pytest.raises(ValueError, match="deadline"):
        bus.send("a", "b", "ping", ok, now=BASE, deadline=BASE)
    with pytest.raises(ValueError, match="deadline"):
        bus.send("a", "b", "ping", ok, now=BASE, deadline=BASE - timedelta(seconds=1))
    with pytest.raises(ValueError, match="deadline"):
        bus.send("a", "b", "ping", ok, now=BASE, deadline=datetime(2026, 1, 2))
    for bad_attempts in (0, -1, 11):
        with pytest.raises(ValueError, match="max_attempts"):
            bus.send("a", "b", "ping", ok, now=BASE, max_attempts=bad_attempts)
    with pytest.raises(ValueError, match="deadline"):
        bus.request("a", "b", "ping", ok, now=BASE, deadline=None)
    # Nothing was persisted by any rejected call.
    assert bus.counts() == {}
    assert store.messages(limit=None) == []


def test_payload_size_boundary(tmp_path):
    store, bus = make_bus(tmp_path)
    overhead = len(json.dumps({"b": ""}).encode("utf-8"))
    fits = {"b": "x" * (32_768 - overhead)}
    receipt = bus.send("a", "b", "bulk.ok", fits, now=BASE)
    assert store.get_message(receipt.message_ids[0])["payload"] == fits
    too_big = {"b": "x" * (32_768 - overhead + 1)}
    with pytest.raises(ValueError, match="payload"):
        bus.send("a", "b", "bulk.no", too_big, now=BASE)
    assert bus.counts() == {"queued": 1}


def test_expire_due_and_deadline_filtering(tmp_path):
    store, bus = make_bus(tmp_path)
    deadline = BASE + timedelta(minutes=5)
    doomed = bus.request("a", "b", "task.request", {"q": 1}, now=BASE, deadline=deadline)
    keeper = bus.send("a", "b", "task.keep", {}, now=BASE + timedelta(seconds=1))

    assert len(bus.inbox("b", now=BASE + timedelta(seconds=1))) == 2
    later = BASE + timedelta(minutes=10)
    # Past-deadline rows disappear from the inbox even before a sweep runs.
    assert [m.id for m in bus.inbox("b", now=later)] == [keeper.message_ids[0]]

    assert bus.expire_due(now=later) == 1
    assert store.get_message(doomed.message_ids[0])["status"] == "expired"
    assert [m.id for m in bus.inbox("b", now=later)] == [keeper.message_ids[0]]
    assert bus.counts() == {"queued": 1, "expired": 1}
    assert bus.outstanding(doomed.correlation_id) == 0

    expired_events = events_of(store, "comms_expired")
    assert len(expired_events) == 1
    assert expired_events[0]["payload"] == {"count": 1}
    # Second sweep is a no-op and emits nothing new.
    assert bus.expire_due(now=later) == 0
    assert len(events_of(store, "comms_expired")) == 1


def test_fail_retries_then_dead_letters(tmp_path):
    store, bus = make_bus(tmp_path)
    secret = "tovarisch-731"
    bus.send("a", "b", "risky.task", {"secret": secret}, now=BASE, max_attempts=3)
    msg = bus.inbox("b", now=BASE)[0]

    assert bus.fail(msg, "boom-1", now=BASE + timedelta(seconds=1)) == "queued"
    row = store.get_message(msg.id)
    assert row["attempts"] == 1
    assert row["status"] == "queued"
    assert len(bus.inbox("b", now=BASE + timedelta(seconds=2))) == 1

    assert bus.fail(msg, "boom-2", now=BASE + timedelta(seconds=2)) == "queued"
    assert store.get_message(msg.id)["attempts"] == 2
    assert events_of(store, "comms_dead_letter") == []

    assert bus.fail(msg, "boom-3", now=BASE + timedelta(seconds=3)) == "dead"
    row = store.get_message(msg.id)
    assert row["attempts"] == 3
    assert row["status"] == "dead"
    assert row["error"] == "boom-3"
    # update_message never touches the payload or routing columns.
    assert row["payload"] == {"secret": secret}
    assert row["kind"] == "risky.task"
    assert bus.inbox("b", now=BASE + timedelta(seconds=4)) == []
    assert bus.counts() == {"dead": 1}

    dead_events = events_of(store, "comms_dead_letter")
    assert len(dead_events) == 1
    assert dead_events[0]["payload"] == {
        "id": msg.id,
        "kind": "risky.task",
        "recipient": "b",
        "error": "boom-3",
    }
    # Audit events must never leak payload contents.
    assert secret not in json.dumps(store.events(limit=1000))


def test_dead_letter_immediately_kills_message(tmp_path):
    store, bus = make_bus(tmp_path)
    bus.send("a", "b", "poison.pill", {"secret": "kolobok-9"}, now=BASE)
    msg = bus.inbox("b", now=BASE)[0]
    assert bus.dead_letter(msg, "unroutable", now=BASE + timedelta(seconds=1)) == "dead"
    row = store.get_message(msg.id)
    assert row["status"] == "dead"
    assert row["error"] == "unroutable"
    events = events_of(store, "comms_dead_letter")
    assert len(events) == 1
    assert events[0]["payload"]["id"] == msg.id
    assert "kolobok-9" not in json.dumps(store.events(limit=1000))
    assert bus.inbox("b", now=BASE + timedelta(seconds=2)) == []


def test_ack_and_fail_require_queued_rows(tmp_path):
    store, bus = make_bus(tmp_path)
    bus.send("a", "b", "task.one", {}, now=BASE)
    msg = bus.inbox("b", now=BASE)[0]
    bus.ack(msg.id, now=BASE)
    with pytest.raises(ValueError, match="ack"):
        bus.ack(msg.id, now=BASE)
    with pytest.raises(ValueError, match="fail"):
        bus.fail(msg, "late", now=BASE)
    with pytest.raises(ValueError, match="dead-letter"):
        bus.dead_letter(msg, "late", now=BASE)
    with pytest.raises(KeyError):
        bus.ack("msg_missing", now=BASE)
    assert bus.counts() == {"done": 1}


def test_message_counts_reflect_statuses(tmp_path):
    store, bus = make_bus(tmp_path)
    bus.send("a", ["b", "c"], "job.offer", {}, now=BASE)
    acked = bus.send("b", "a", "fyi.note", {}, now=BASE)
    bus.ack(acked.message_ids[0], now=BASE)
    bus.send("c", "d", "flaky.ping", {}, now=BASE, max_attempts=1)
    bus.fail(bus.inbox("d", now=BASE)[0], "no answer", now=BASE)
    bus.request("d", "a", "slow.request", {}, now=BASE, deadline=BASE + timedelta(minutes=1))
    bus.expire_due(now=BASE + timedelta(minutes=2))
    expected = {"queued": 2, "done": 1, "dead": 1, "expired": 1}
    assert bus.counts() == expected
    assert store.message_counts() == expected


def test_concurrent_sends_produce_exact_row_counts(tmp_path):
    store, bus = make_bus(tmp_path)
    sends = 32

    def worker(i: int) -> SendReceipt:
        return bus.send("a", ("b", "c"), "load.test", {"i": i}, now=BASE)

    with ThreadPoolExecutor(max_workers=8) as pool:
        futures = [pool.submit(worker, i) for i in range(sends)]
        receipts = [f.result() for f in futures]

    ids = [mid for r in receipts for mid in r.message_ids]
    assert len(ids) == sends * 2
    assert len(set(ids)) == sends * 2
    assert len({r.correlation_id for r in receipts}) == sends
    assert len(store.messages(recipient="b", limit=None)) == sends
    assert len(store.messages(recipient="c", limit=None)) == sends
    assert bus.counts() == {"queued": sends * 2}
    assert len(bus.inbox("b", now=BASE)) == 20  # default inbox limit
    assert len(bus.inbox("b", now=BASE, limit=100)) == sends
