from sovereign.config import EngineConfig
from sovereign.engine.world import bootstrap


def test_tool_allow_and_deny(tmp_path):
    cfg = EngineConfig(mode="sim", data_dir=tmp_path)  # type: ignore[arg-type]
    world = bootstrap(cfg)
    names = world.tools.names()
    assert "jobs.search" in names
    assert "heal.repair" in names
    assert "mail.send" in names
    assert "invoice.issue" in names
    hunt = world.use_tool("hunter", "jobs.search", live=False)
    assert hunt.ok
    assert isinstance(hunt.data, list)
    denied = world.use_tool("hunter", "governance.freeze", target="closer", reason="no")
    assert not denied.ok
    assert "denied" in (denied.error or "")
    events = world.store.events(40)
    assert any(e["kind"] == "tool_denied" for e in events)
    # kwargs with secrets must not land in the event payload
    blob = str(events)
    assert "reason" not in blob or "mnemonic" not in blob
    mechanic_tools = world.tools.available_to("mechanic")
    assert "heal.repair" in mechanic_tools
    assert "governance.thaw" in mechanic_tools
    closer_tools = world.tools.available_to("closer")
    assert "brain.complete" in closer_tools
    assert "heal.repair" not in closer_tools
    wrote = world.use_tool("improver", "playbook.write_trial", agent="closer", body="# trial\n")
    assert wrote.ok
    assert (tmp_path / "playbooks" / "closer.trial.md").exists()


def test_cli_tools(tmp_path, capsys):
    from sovereign.cli import main

    assert main(["init", "--data-dir", str(tmp_path)]) == 0
    capsys.readouterr()
    assert main(["tools", "--data-dir", str(tmp_path), "--agent", "mechanic"]) == 0
    out = capsys.readouterr().out
    assert "heal.repair" in out
