"""Agent specs are the single source of truth: prompts, tiers, and tool
permissions in sovereign.agents.spec must match what the registry enforces."""

import pytest

from sovereign.agents.spec import (
    AGENT_SPECS,
    roster,
    spec_for,
    system_prompt_for,
    tool_matrix,
)
from sovereign.config import EngineConfig
from sovereign.engine.world import bootstrap
from sovereign.memory.playbooks import DEFAULT_PLAYBOOKS
from sovereign.tools import catalog

EXPECTED_AGENTS = frozenset(
    {
        "mechanic",
        "bookkeeper",
        "risk",
        "ethics",
        "director",
        "hunter",
        "closer",
        "crafter",
        "trader",
        "publisher",
        "scout",
        "operator",
        "treasurer",
        "auditor",
        "improver",
        "courier",
    }
)
VOTERS = frozenset({"treasurer", "risk", "director"})
DRIFT_FIXES = (
    ("trader", "ledger.snapshot"),
    ("ethics", "mail.list"),
    ("operator", "ledger.snapshot"),
    ("scout", "ledger.snapshot"),
    ("publisher", "files.list_work"),
)
DENIED_PAIRS = (
    ("hunter", "governance.freeze"),
    ("closer", "heal.repair"),
    ("mechanic", "invoice.collect"),
)


@pytest.fixture()
def world(tmp_path):
    cfg = EngineConfig(mode="sim", data_dir=tmp_path)  # type: ignore[arg-type]
    return bootstrap(cfg)


def test_all_sixteen_specs_are_complete():
    assert roster() == EXPECTED_AGENTS
    for name in sorted(EXPECTED_AGENTS):
        spec = spec_for(name)
        assert spec.name == name
        assert spec.mission.strip(), f"{name} has an empty mission"
        assert spec.tier in (None, "fast", "work", "think")
        lines = [line for line in spec.system_prompt.splitlines() if line.strip()]
        assert len(lines) >= 12, f"{name} prompt has only {len(lines)} lines"
        for tool in spec.tools:
            assert tool in spec.system_prompt, f"{name} prompt does not mention {tool}"
        assert "wallet.public" in spec.tools
        assert "playbook.read" in spec.tools
        assert spec.handles[:2] == ("ping", "notify")
        if name in VOTERS:
            assert "vote_request" in spec.handles
            assert "vote_request" in spec.system_prompt
        else:
            assert "vote_request" not in spec.handles
    with pytest.raises(KeyError):
        spec_for("intruder")


def test_tool_matrix_matches_registry_for_every_agent(world):
    matrix = tool_matrix()
    for agent in sorted(roster()):
        from_registry = sorted(world.tools.available_to(agent))
        from_specs = sorted(spec_for(agent).tools)
        from_matrix = sorted(name for name, allowed in matrix.items() if agent in allowed)
        assert from_registry == from_specs, f"registry drifted from spec for {agent}"
        assert from_matrix == from_specs, f"tool_matrix drifted from spec for {agent}"
    assert sorted(matrix) == world.tools.names()


def test_intentional_drift_fixes_are_granted(world):
    for agent, tool in DRIFT_FIXES:
        assert tool in world.tools.available_to(agent), f"{agent} should now have {tool}"
    snap = world.use_tool("trader", "ledger.snapshot")
    assert snap.ok and "equity_usd" in snap.data
    assert world.use_tool("ethics", "mail.list").ok
    assert world.use_tool("operator", "ledger.snapshot").ok
    assert world.use_tool("scout", "ledger.snapshot").ok
    listing = world.use_tool("publisher", "files.list_work", job_id="job_specs00001")
    assert listing.ok and listing.data == []


def test_previously_denied_pairs_stay_denied(world):
    for agent, tool in DENIED_PAIRS:
        assert tool not in world.tools.available_to(agent)
        result = world.use_tool(agent, tool)
        assert not result.ok
        assert "denied" in (result.error or "")


def test_playbook_tools_lines_only_name_callable_tools(world):
    preface = "Tactical playbook (editable data layered under the fixed system prompt)."
    assert set(DEFAULT_PLAYBOOKS) == EXPECTED_AGENTS
    for agent, body in DEFAULT_PLAYBOOKS.items():
        assert body.splitlines()[0] == preface, f"{agent} playbook is missing the preface"
        allowed = set(world.tools.available_to(agent))
        tool_lines = [line for line in body.splitlines() if "Tools:" in line]
        assert tool_lines, f"{agent} playbook has no Tools line"
        for line in tool_lines:
            names = [n.strip() for n in line.split("Tools:", 1)[1].split(",") if n.strip()]
            assert names
            for name in names:
                assert name in allowed, f"{agent} playbook names {name}, which it cannot call"


def test_brain_complete_ignores_caller_supplied_system(world, monkeypatch):
    captured = {}

    def capture(prompt, tier="fast", system="default"):
        captured.update({"prompt": prompt, "tier": tier, "system": system})
        return "captured-completion"

    monkeypatch.setattr(world.router, "complete", capture)
    result = world.use_tool("closer", "brain.complete", prompt="x", system="EVIL OVERRIDE")
    assert result.ok and result.data == "captured-completion"
    assert captured["system"] == system_prompt_for("closer")
    assert "EVIL" not in captured["system"]
    assert captured["tier"] == "work"  # closer's spec tier fills in when unspecified

    captured.clear()
    explicit = world.use_tool("director", "brain.complete", prompt="y", tier="think", system="ignored")
    assert explicit.ok
    assert captured["tier"] == "think"
    assert captured["system"] == system_prompt_for("director")

    # A smuggled "caller" kwarg must not let one agent borrow another's prompt.
    captured.clear()
    spoofed = world.use_tool("closer", "brain.complete", prompt="z", caller="director")
    assert spoofed.ok
    assert captured["system"] == system_prompt_for("closer")


def test_registry_raises_on_spec_catalog_drift(monkeypatch):
    healthy = tool_matrix()

    with_unknown = dict(healthy)
    with_unknown["quantum.entangle"] = frozenset({"closer"})
    monkeypatch.setattr(catalog, "tool_matrix", lambda: with_unknown)
    with pytest.raises(RuntimeError, match="quantum.entangle"):
        catalog.build_registry()

    without_repair = {name: allowed for name, allowed in healthy.items() if name != "heal.repair"}
    monkeypatch.setattr(catalog, "tool_matrix", lambda: without_repair)
    with pytest.raises(RuntimeError, match="heal.repair"):
        catalog.build_registry()

    with pytest.raises(RuntimeError, match="empty allowlist"):
        catalog.validate_matrix({"heal.repair": frozenset()}, ["heal.repair"])


def test_specs_cover_all_playbook_agents_and_bus_roster():
    # The comms bus takes roster() verbatim; every spec name must be usable.
    names = roster()
    assert all(isinstance(n, str) and n for n in names)
    assert len(AGENT_SPECS) == 16
