"""`sovereign bootstrap` / `sovereign agents` and the readiness report."""

import json

from sovereign.agents.spec import spec_for, system_prompt_for
from sovereign.cli import main
from sovereign.config import EngineConfig
from sovereign.engine.world import bootstrap
from sovereign.heal.checks import diagnose
from sovereign.ops import readiness

REQUIRED_ENTRY_KEYS = {"name", "mission", "tier", "tools", "handles", "frozen", "inbox_queued"}


def test_bootstrap_command_is_ready_in_sim(tmp_path, capsys):
    code = main(["bootstrap", "--data-dir", str(tmp_path), "--mode", "sim"])
    out = capsys.readouterr().out
    assert code == 0
    payload = json.loads(out)

    report = payload["readiness"]
    assert report["ready"] is True
    assert report["mode"] == "sim"
    by_name = {check["name"]: check for check in report["checks"]}
    for check in report["checks"]:
        assert set(check) == {"name", "ok", "required", "detail"}

    roundtrip = by_name["comms_roundtrip"]
    assert roundtrip["ok"] is True
    assert roundtrip["required"] is True
    assert by_name["python_version"]["ok"] is True
    assert by_name["engine_health"]["ok"] is True
    assert by_name["tools_and_specs"]["ok"] is True
    assert by_name["model_provider"]["detail"] == "sim brain"

    assert payload["identity"]["eth"].startswith("0x")
    assert payload["wallet"]["eth_address"].startswith("0x")
    assert payload["wallet"]["sol_address"]


def test_readiness_leaves_no_queued_messages(tmp_path):
    cfg = EngineConfig(mode="sim", data_dir=tmp_path)  # type: ignore[arg-type]
    world = bootstrap(cfg)
    report = readiness(world)
    assert report["ready"] is True
    assert world.store.messages(status="queued", limit=None) == []
    assert world.store.message_counts().get("queued", 0) == 0


def test_agents_command_lists_exactly_sixteen(tmp_path, capsys):
    code = main(["agents", "--data-dir", str(tmp_path), "--mode", "sim"])
    assert code == 0
    entries = json.loads(capsys.readouterr().out)
    assert len(entries) == 16
    assert {entry["name"] for entry in entries} == {
        "mechanic", "bookkeeper", "risk", "ethics", "director", "hunter",
        "closer", "crafter", "trader", "publisher", "scout", "operator",
        "treasurer", "auditor", "improver", "courier",
    }
    for entry in entries:
        assert REQUIRED_ENTRY_KEYS <= set(entry)
        assert "system_prompt" not in entry  # list view stays compact
        assert entry["frozen"] is False
        assert entry["inbox_queued"] == 0
        assert entry["tools"]
        assert entry["handles"][:2] == ["ping", "notify"]


def test_agents_command_single_agent_includes_prompt_and_tools(tmp_path, capsys):
    code = main(["agents", "--data-dir", str(tmp_path), "--mode", "sim", "--agent", "closer"])
    assert code == 0
    entry = json.loads(capsys.readouterr().out)
    assert entry["name"] == "closer"
    assert entry["system_prompt"] == system_prompt_for("closer")
    assert entry["system_prompt"].startswith("You are CLOSER")
    # Tools come from the registry, which is derived from the same spec.
    assert sorted(entry["tools"]) == sorted(spec_for("closer").tools)
    assert "mail.send" in entry["tools"]
    assert "brain.complete" in entry["tools"]


def test_agents_command_unknown_agent_fails_with_error_envelope(tmp_path, capsys):
    code = main(["agents", "--data-dir", str(tmp_path), "--mode", "sim", "--agent", "wizard"])
    assert code == 1
    captured = capsys.readouterr()
    envelope = json.loads(captured.err)
    assert envelope["ok"] is False
    assert "wizard" in envelope["error"]
    assert "Traceback" not in captured.err


def test_diagnose_includes_ok_comms_finding_on_fresh_world(tmp_path):
    cfg = EngineConfig(mode="sim", data_dir=tmp_path)  # type: ignore[arg-type]
    world = bootstrap(cfg)
    findings = {finding.code: finding for finding in diagnose(world)}
    assert "comms" in findings
    assert findings["comms"].ok is True
    assert findings["comms"].repairable is False
