"""MCP bridge wired into the engine: EngineConfig.mcp, world lifecycle, the
spec-derived mcp.list/mcp.call grants, the per-(tick, server) rate cap,
secret/argument hygiene, and the status/readiness/heal paths that must never
connect.

Everything runs against `sovereign.mcp.fakes.FakeTransport` — no `mcp` SDK,
no subprocesses — injected through the registry's public `transport_factory`.
"""

from __future__ import annotations

import json

import pytest

from sovereign.agents.spec import tool_matrix
from sovereign.config import EngineConfig, McpConfig, McpServerConfig
from sovereign.engine.world import bootstrap
from sovereign.heal.checks import diagnose
from sovereign.mcp.client import UNTRUSTED_BEGIN, UNTRUSTED_END
from sovereign.mcp.fakes import FakeTransport
from sovereign.mcp.transport import McpResult, McpToolSpec
from sovereign.ops import readiness
from sovereign.tools import catalog

MCP_TOOLS = frozenset({"mcp.list", "mcp.call"})
GRANTED = ("hunter", "closer", "crafter", "publisher", "scout", "operator")
SECRET = "sk-live-SUPERSECRET-4242"


def scripted_tools() -> list[McpToolSpec]:
    return [
        McpToolSpec(
            server="",
            name="generate_image",
            description="Render a hero image",
            input_schema={"type": "object"},
        ),
        McpToolSpec(
            server="",
            name="search_market",
            description="Search demand signals",
            input_schema={"type": "object"},
        ),
    ]


def make_world(
    tmp_path,
    *,
    enabled: bool = True,
    calls_per_tick: int = 2,
    allow_agents: tuple[str, ...] = ("closer", "crafter", "scout"),
    handler=None,
):
    cfg = EngineConfig(
        mode="sim",
        data_dir=tmp_path,
        public_job_apis=False,
        fetch_market_data=False,
        mcp=McpConfig(
            enabled=enabled,
            servers=(
                McpServerConfig(
                    name="studio",
                    transport="stdio",
                    command="fake-design-server",
                    env_credentials={"DESIGN_API_KEY": "DESIGN_API_KEY"},
                    allow_agents=allow_agents,
                    calls_per_tick=calls_per_tick,
                ),
            ),
        ),
    )  # type: ignore[arg-type]
    world = bootstrap(cfg)
    transports: list[FakeTransport] = []
    factory_calls: list[str] = []

    def factory(server, *, secret_resolver):
        factory_calls.append(server.name)
        transport = FakeTransport(scripted_tools(), handler)
        transports.append(transport)
        return transport

    world.mcp.transport_factory = factory
    return world, transports, factory_calls


# -- fail-closed gates ---------------------------------------------------------


def test_mcp_disabled_fails_closed_without_connecting(tmp_path):
    world, transports, factory_calls = make_world(tmp_path, enabled=False)

    listed = world.use_tool("closer", "mcp.list")
    assert listed.ok
    assert listed.data == []

    called = world.use_tool(
        "closer", "mcp.call", server="studio", tool="generate_image", arguments={}
    )
    assert not called.ok
    assert called.error == "mcp disabled"

    assert factory_calls == []  # nothing ever connected
    assert transports == []
    assert world.status()["mcp"] == {
        "enabled": False,
        "servers": ["studio"],
        "errors": 0,
    }


def test_non_granted_agent_is_denied_by_registry_allowlist(tmp_path):
    world, _transports, factory_calls = make_world(tmp_path)

    for name, kwargs in (
        ("mcp.list", {}),
        ("mcp.call", {"server": "studio", "tool": "generate_image"}),
    ):
        denied = world.use_tool("risk", name, **kwargs)
        assert not denied.ok, name
        assert "denied" in (denied.error or "")

    assert factory_calls == []  # denial happens before any transport is built
    assert any(e["kind"] == "tool_denied" for e in world.store.events(20))


def test_arguments_must_be_a_dict(tmp_path):
    world, transports, _ = make_world(tmp_path)
    bad = world.use_tool(
        "closer", "mcp.call", server="studio", tool="search_market", arguments="q=x"
    )
    assert not bad.ok
    assert bad.error == "arguments must be a dict"
    assert transports == []  # rejected before the registry is touched


# -- the granted path -----------------------------------------------------------


def test_granted_closer_lists_and_calls_with_fenced_result(tmp_path):
    world, transports, _ = make_world(tmp_path)

    listed = world.use_tool("closer", "mcp.list")
    assert listed.ok
    assert [(t["server"], t["name"]) for t in listed.data] == [
        ("studio", "generate_image"),
        ("studio", "search_market"),
    ]
    assert all(set(t) == {"server", "name", "description"} for t in listed.data)

    called = world.use_tool(
        "closer",
        "mcp.call",
        server="studio",
        tool="generate_image",
        arguments={"prompt": "logo hero"},
    )
    assert called.ok
    data = called.data
    assert data["server"] == "studio"
    assert data["tool"] == "generate_image"
    assert data["ok"] is True
    assert data["error"] is None
    assert data["content"].startswith(UNTRUSTED_BEGIN + "\n")
    assert data["content"].endswith("\n" + UNTRUSTED_END)
    assert set(data) == {"server", "tool", "ok", "content", "error"}
    assert transports[0].calls == [("generate_image", {"prompt": "logo hero"})]


def test_arguments_default_to_empty_dict(tmp_path):
    world, transports, _ = make_world(tmp_path)
    called = world.use_tool("closer", "mcp.call", server="studio", tool="search_market")
    assert called.ok
    assert transports[0].calls == [("search_market", {})]


# -- rate cap ---------------------------------------------------------------------


def test_per_tick_server_cap_raises_past_budget_and_resets(tmp_path):
    world, _, _ = make_world(tmp_path, calls_per_tick=2)
    world.start_tick()

    for _ in range(2):
        ok = world.use_tool(
            "closer", "mcp.call", server="studio", tool="search_market", arguments={"q": "x"}
        )
        assert ok.ok

    over = world.use_tool(
        "closer", "mcp.call", server="studio", tool="search_market", arguments={"q": "x"}
    )
    assert not over.ok
    assert over.error == "mcp rate cap reached for studio"
    assert world.store.get_kv("mcp_call_guard") == {
        "tick": world.tick,
        "counts": {"studio": 2},
    }

    world.start_tick()  # a new tick resets the counts
    again = world.use_tool(
        "closer", "mcp.call", server="studio", tool="search_market", arguments={"q": "x"}
    )
    assert again.ok
    assert world.store.get_kv("mcp_call_guard") == {
        "tick": world.tick,
        "counts": {"studio": 1},
    }


def test_unknown_server_rejected_without_consuming_budget(tmp_path):
    world, _, factory_calls = make_world(tmp_path)
    missing = world.use_tool(
        "closer", "mcp.call", server="nowhere", tool="search_market", arguments={}
    )
    assert missing.ok  # defensive: the registry reports, never raises
    assert missing.data["ok"] is False
    assert missing.data["error"] == "unknown server"
    assert factory_calls == []
    assert world.store.get_kv("mcp_call_guard") is None


# -- secret & argument hygiene ------------------------------------------------------


def test_arguments_and_secrets_never_reach_events_or_the_return(tmp_path):
    world, transports, _ = make_world(
        tmp_path, handler=lambda name, args: McpResult(ok=True, text="hero drawn")
    )
    world.wallet.put_credential("DESIGN_API_KEY", SECRET)
    marker = "argument-marker-1b9f"

    called = world.use_tool(
        "closer",
        "mcp.call",
        server="studio",
        tool="generate_image",
        arguments={"prompt": marker, "token": SECRET},
    )
    assert called.ok
    # Arguments were delivered to the transport but appear nowhere else.
    assert transports[0].calls == [
        ("generate_image", {"prompt": marker, "token": SECRET})
    ]
    assert marker not in json.dumps(called.data)
    assert SECRET not in json.dumps(called.data)
    blob = json.dumps(world.store.events(300))
    assert marker not in blob
    assert SECRET not in blob


# -- lifecycle & status ----------------------------------------------------------


def test_finish_tick_closes_registry_and_clears_cache(tmp_path):
    world, transports, factory_calls = make_world(tmp_path)
    world.start_tick()

    assert world.use_tool("closer", "mcp.list").ok  # builds the one client
    assert len(transports) == 1
    assert transports[0].closed is False

    world.finish_tick()
    assert transports[0].closed is True

    before = len(factory_calls)
    assert world.use_tool("closer", "mcp.list").ok  # cache cleared: reconnects
    assert len(factory_calls) == before + 1


def test_status_reports_mcp_without_connecting(tmp_path):
    world, _, factory_calls = make_world(tmp_path)
    status = world.status()["mcp"]
    assert status == {"enabled": True, "servers": ["studio"], "errors": 0}
    assert factory_calls == []  # status() never builds a transport


def test_readiness_and_diagnose_never_connect(tmp_path):
    world, _, factory_calls = make_world(tmp_path)

    report = readiness(world)
    assert report["ready"] is True
    by_name = {check["name"]: check for check in report["checks"]}
    mcp_check = by_name["mcp"]
    assert mcp_check["required"] is False
    assert mcp_check["ok"] is True
    assert mcp_check["detail"]["enabled"] is True
    assert mcp_check["detail"]["servers"] == 1
    assert mcp_check["detail"]["errors"] == 0
    assert "sdk" in mcp_check["detail"]

    findings = {finding.code: finding for finding in diagnose(world)}
    assert findings["mcp"].ok is True
    assert findings["mcp"].repairable is False
    assert "enabled=True" in findings["mcp"].detail
    assert "servers=1" in findings["mcp"].detail

    assert factory_calls == []  # neither probe ever built a transport


# -- spec / registry consistency ------------------------------------------------


def test_spec_registry_consistency_and_matrix(tmp_path):
    world, _, _ = make_world(tmp_path)
    assert MCP_TOOLS <= set(world.tools.names())

    matrix = tool_matrix()
    catalog.validate_matrix(matrix, (n for n, _, _, _ in catalog._tool_defs()))

    for agent in GRANTED:
        assert MCP_TOOLS <= set(world.tools.available_to(agent)), agent
    assert "mcp.call" in world.tools.available_to("closer")
    for agent in ("risk", "bookkeeper", "treasurer", "trader", "ethics"):
        assert not MCP_TOOLS & set(world.tools.available_to(agent)), agent

    broken = {n: a for n, a in matrix.items() if n != "mcp.call"}
    with pytest.raises(RuntimeError, match="mcp.call"):
        catalog.validate_matrix(broken, (n for n, _, _, _ in catalog._tool_defs()))


# -- CLI ---------------------------------------------------------------------------


def test_cli_mcp_lists_config_without_secrets_and_probe_uses_fakes(
    tmp_path, capsys, monkeypatch
):
    from sovereign.cli import main

    (tmp_path).mkdir(parents=True, exist_ok=True)
    (tmp_path / "config.yaml").write_text(
        "mcp:\n"
        "  enabled: true\n"
        "  servers:\n"
        "    - name: studio\n"
        "      transport: stdio\n"
        "      command: fake-design-server\n"
        "      env_credentials: {DESIGN_API_KEY: DESIGN_API_KEY}\n"
        "      allow_agents: [closer]\n"
        "      calls_per_tick: 3\n"
    )

    assert main(["mcp", "--data-dir", str(tmp_path)]) == 0
    out = capsys.readouterr().out
    payload = json.loads(out)
    assert payload["enabled"] is True
    assert payload["servers"] == [
        {
            "name": "studio",
            "transport": "stdio",
            "allow_agents": ["closer"],
            "allowed_tools": [],
            "calls_per_tick": 3,
        }
    ]
    # No connect details, credential refs, or secrets in the config view.
    assert "fake-design-server" not in out
    assert "DESIGN_API_KEY" not in out

    def fake_factory(server, *, secret_resolver):
        return FakeTransport(scripted_tools())

    monkeypatch.setattr("sovereign.mcp.registry.build_transport", fake_factory)
    assert main(["mcp", "--data-dir", str(tmp_path), "--probe"]) == 0
    probed = json.loads(capsys.readouterr().out)
    assert probed["errors"] == []
    assert [t["name"] for t in probed["tools"]["studio"]] == [
        "generate_image",
        "search_market",
    ]
