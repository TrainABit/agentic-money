"""New agent capabilities: financial invariants, knowledge memory flow,
ledger export, rate-capped notifies, and transition-only health messaging.

Everything runs in sim mode against tmp_path stores, driving roles directly
or through heartbeat.step, so every scenario is deterministic.
"""

import csv
import sqlite3

from sovereign.agents import roles
from sovereign.agents.spec import AGENT_SPECS, roster, tool_matrix
from sovereign.capital.invariants import CHECK_NAMES, verify_invariants
from sovereign.config import EngineConfig
from sovereign.engine.heartbeat import step
from sovereign.engine.world import bootstrap
from sovereign.memory.knowledge import KNOWLEDGE_HEADER
from sovereign.tools.catalog import NOTIFY_CAP_PER_TICK


def _world(tmp_path, ticks: int = 0):
    cfg = EngineConfig(mode="sim", data_dir=tmp_path)  # type: ignore[arg-type]
    world = bootstrap(cfg)
    for _ in range(ticks):
        step(world)
    return world


def _delete_issue_row(world) -> float:
    """Tamper with the books: remove one invoice-issue posting via raw SQL.

    Uses a separate connection so the store's data_version bumps and the
    ledger balance cache cannot serve stale numbers.
    """
    conn = sqlite3.connect(world.store.db_path)
    try:
        row = conn.execute(
            "SELECT id, amount FROM ledger WHERE debit='assets.receivable' "
            "AND credit='liability.unearned' ORDER BY id ASC LIMIT 1"
        ).fetchone()
        assert row is not None, "expected at least one invoice-issue ledger row"
        conn.execute("DELETE FROM ledger WHERE id=?", (row[0],))
        conn.commit()
        return float(row[1])
    finally:
        conn.close()


# ---------------------------------------------------------------------------
# Invariants
# ---------------------------------------------------------------------------


def test_invariants_pass_fresh_then_break_on_deleted_ledger_row(tmp_path):
    world = _world(tmp_path, ticks=12)
    report = verify_invariants(world)
    assert [c["name"] for c in report["checks"]] == list(CHECK_NAMES)
    assert report["ok"], report

    _delete_issue_row(world)
    tampered = verify_invariants(world)
    assert tampered["ok"] is False
    failed = {c["name"] for c in tampered["checks"] if not c["ok"]}
    assert "accounting_identity" in failed
    assert "receivable_matches_open_invoices" in failed
    for check in tampered["checks"]:
        assert check["detail"], check


def test_auditor_cadence_surfaces_breach_and_notifies(tmp_path):
    world = _world(tmp_path, ticks=12)
    _delete_issue_row(world)
    world.start_tick()  # fresh tick: auditor cadence due, fresh notify budget

    action = roles.auditor(world)[0]
    assert action["kind"] == "audit"
    assert action["invariants"] is not None
    assert action["invariants"]["ok"] is False
    findings = [n for n in action["notes"] if isinstance(n, dict) and "invariants" in n]
    assert findings and "accounting_identity" in findings[0]["invariants"]

    for seat in ("treasurer", "risk"):
        notifies = [
            m
            for m in world.store.messages(recipient=seat, limit=None)
            if m["kind"] == "notify" and m["payload"].get("event") == "invariant_breach"
        ]
        assert notifies, f"{seat} never received the breach notify"
        assert all(m["status"] in {"queued", "done"} for m in notifies)

    firm = world.store.recent_knowledge("firm", 10)
    assert any(r["topic"] == "invariant_breach" for r in firm)


# ---------------------------------------------------------------------------
# Ledger export
# ---------------------------------------------------------------------------


def test_ledger_export_writes_csv_and_hunter_is_denied(tmp_path):
    world = _world(tmp_path, ticks=3)
    res = world.use_tool("bookkeeper", "ledger.export")
    assert res.ok, res.error
    rows = world.store.ledger_rows()
    assert res.data["rows"] == len(rows)
    with open(res.data["path"], newline="") as fh:
        lines = list(csv.reader(fh))
    assert lines[0] == ["ts", "debit", "credit", "amount", "memo", "ref"]
    assert len(lines) - 1 == len(rows)

    denied = world.use_tool("hunter", "ledger.export")
    assert not denied.ok
    assert "denied" in (denied.error or "")


def test_bookkeeper_exports_on_cadence(tmp_path):
    world = _world(tmp_path)  # tick 0: the 30-tick export cadence is due
    action = roles.bookkeeper(world)[0]
    assert action["kind"] == "snapshot"
    assert action.get("export_path"), action
    assert action["export_rows"] == len(world.store.ledger_rows())


# ---------------------------------------------------------------------------
# comms.notify
# ---------------------------------------------------------------------------


def test_comms_notify_cap_denial_and_validation(tmp_path):
    world = _world(tmp_path)
    world.start_tick()
    for i in range(NOTIFY_CAP_PER_TICK):
        sent = world.use_tool("risk", "comms.notify", recipients="director", payload={"n": i})
        assert sent.ok, sent.error
    # The cap is shared across ALL callers within one tick.
    capped = world.use_tool("auditor", "comms.notify", recipients="director", payload={"n": 5})
    assert not capped.ok
    assert "notify rate cap reached" in (capped.error or "")

    world.start_tick()  # budget resets with the tick
    reset = world.use_tool("risk", "comms.notify", recipients="director", payload={"reset": True})
    assert reset.ok, reset.error

    denied = world.use_tool("hunter", "comms.notify", recipients="director", payload={})
    assert not denied.ok
    assert "denied" in (denied.error or "")

    bad_type = world.use_tool("risk", "comms.notify", recipients=123, payload={})
    assert not bad_type.ok
    assert "recipients" in (bad_type.error or "")
    unknown = world.use_tool("risk", "comms.notify", recipients="nobody", payload={})
    assert not unknown.ok
    assert "roster" in (unknown.error or "")
    bad_payload = world.use_tool("risk", "comms.notify", recipients="director", payload="hi")
    assert not bad_payload.ok
    assert "payload" in (bad_payload.error or "")


# ---------------------------------------------------------------------------
# Knowledge flow
# ---------------------------------------------------------------------------


def test_knowledge_flow_end_to_end(tmp_path):
    world = _world(tmp_path, ticks=18)
    closer_rows = world.store.recent_knowledge("closer", 100)
    assert any(r["topic"] in {"won_job", "lost_job"} for r in closer_rows)

    crafter_rows = world.store.recent_knowledge("crafter", 100)
    deliveries = [r for r in crafter_rows if r["topic"] == "delivery"]
    assert deliveries
    assert all("entry=" in r["content"] for r in deliveries)

    treasurer_rows = world.store.recent_knowledge("treasurer", 100)
    payments = [r for r in treasurer_rows if r["topic"] == "payment"]
    assert payments
    assert all(" | via " in r["content"] and "$" in r["content"] for r in payments)


def test_closer_prompt_contains_knowledge_block(tmp_path, monkeypatch):
    world = _world(tmp_path)
    world.start_tick()
    world.knowledge.remember(
        "closer",
        "won_job",
        "Webhook automation sprint | $400 | fit=0.8",
        now=world.now,
    )
    world.store.upsert_job(
        {
            "id": "job_knowledge0",
            "source": "sim",
            "title": "Automation sprint: webhook to spreadsheet",
            "description": "Build a webhook to spreadsheet automation.",
            "status": "open",
            "fit": 0.9,
            "contact": "client@sim.local",
        }
    )
    captured: list[str] = []
    real_complete = world.router.complete

    def capture(prompt, tier="fast", system="default"):
        captured.append(prompt)
        return real_complete(prompt, tier=tier, system=system)

    monkeypatch.setattr(world.router, "complete", capture)
    roles.closer(world)

    proposals = [p for p in captured if "Write a short proposal" in p]
    assert proposals
    with_block = [p for p in proposals if KNOWLEDGE_HEADER in p]
    assert with_block, "closer prompt is missing the KNOWLEDGE block"
    assert any("won_job" in p for p in with_block)
    # The block sits after the TACTICS section, never inside it.
    for p in with_block:
        assert p.index("----- END TACTICS -----") < p.index(KNOWLEDGE_HEADER)

    # The sim outcome recorded a compact won/lost lesson for the job.
    lessons = [
        r
        for r in world.store.recent_knowledge("closer", 20)
        if r["topic"] in {"won_job", "lost_job"}
        and r["content"].startswith("Automation sprint: webhook to spreadsheet |")
    ]
    assert lessons


def test_knowledge_remember_writes_only_caller_namespace(tmp_path):
    world = _world(tmp_path)
    world.start_tick()
    res = world.use_tool("closer", "knowledge.remember", topic="won_job", content="test note")
    assert res.ok, res.error
    assert res.data["agent"] == "closer"
    assert world.store.knowledge_count("closer") == 1
    assert world.store.knowledge_count("firm") == 0

    # A smuggled agent kwarg cannot redirect the write to another namespace.
    smuggled = world.use_tool(
        "closer", "knowledge.remember", agent="firm", topic="t", content="c"
    )
    assert not smuggled.ok
    assert world.store.knowledge_count("firm") == 0

    share_denied = world.use_tool("closer", "knowledge.share", topic="t", content="c")
    assert not share_denied.ok
    assert "denied" in (share_denied.error or "")
    assert world.store.knowledge_count("firm") == 0

    shared = world.use_tool("improver", "knowledge.share", topic="t", content="c")
    assert shared.ok, shared.error
    firm = world.store.recent_knowledge("firm", 5)
    assert len(firm) == 1
    assert firm[0]["source"] == "improver"

    recalled = world.use_tool("closer", "knowledge.recall", query="test note", limit=50)
    assert recalled.ok, recalled.error  # limit clamps to 1..10 instead of failing
    assert any(r["agent"] == "closer" for r in recalled.data)


# ---------------------------------------------------------------------------
# Mechanic transition messaging
# ---------------------------------------------------------------------------


def test_mechanic_transition_messaging(tmp_path, monkeypatch):
    world = _world(tmp_path)
    world.certified = [{"strategy_id": "s", "certified": True}]  # skip certify branch
    forced = {"healthy": False}

    def fake_setup(w, full=False):
        return {
            "healthy": forced["healthy"],
            "findings": [],
            "repairs": [],
            "full": full,
            "tick": w.tick,
        }

    monkeypatch.setattr("sovereign.heal.repair.setup", fake_setup)

    def alerts():
        return [
            m
            for m in world.store.messages(limit=None)
            if m["sender"] == "mechanic"
            and m["kind"] == "notify"
            and m["payload"].get("event") == "health_alert"
        ]

    def recoveries():
        return [
            m
            for m in world.store.messages(recipient="director", limit=None)
            if m["kind"] == "notify" and m["payload"].get("event") == "health_recovered"
        ]

    transitions = []
    for _ in range(3):
        world.start_tick()
        transitions.append(roles.mechanic(world)[0]["health_transition"])
    assert transitions == ["alerted", None, None]
    down = alerts()
    assert len({m["correlation_id"] for m in down}) == 1  # exactly one broadcast set
    assert len(down) == len(roster()) - 1  # every peer got the alert once
    assert not recoveries()

    forced["healthy"] = True
    world.start_tick()
    action = roles.mechanic(world)[0]
    assert action["health_transition"] == "recovered"
    assert action["recovery_notify_error"] is None
    assert len(recoveries()) == 1
    assert len({m["correlation_id"] for m in alerts()}) == 1

    # Steady healthy state stays silent.
    world.start_tick()
    assert roles.mechanic(world)[0]["health_transition"] is None
    assert len(recoveries()) == 1


# ---------------------------------------------------------------------------
# Spec / registry consistency
# ---------------------------------------------------------------------------


def test_spec_registry_consistency_with_new_tools(tmp_path):
    world = _world(tmp_path)  # bootstrap runs the drift validator
    available = world.tools.available_to("closer")
    assert "knowledge.remember" in available
    assert "knowledge.recall" in available

    matrix = tool_matrix()
    assert matrix["knowledge.remember"] == roster()
    assert matrix["knowledge.recall"] == roster()
    assert matrix["knowledge.share"] == frozenset({"improver", "auditor", "director", "mechanic"})
    assert matrix["ledger.verify_invariants"] == frozenset(
        {"auditor", "bookkeeper", "risk", "mechanic"}
    )
    assert matrix["ledger.export"] == frozenset({"bookkeeper", "treasurer", "auditor"})
    assert matrix["comms.notify"] == frozenset(
        {"mechanic", "risk", "auditor", "director", "treasurer"}
    )
    for name, spec in AGENT_SPECS.items():
        assert set(world.tools.available_to(name)) == set(spec.tools), name
