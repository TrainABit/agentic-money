"""One wedged agent must never freeze the whole firm.

Unit level: the watchdog returns fast results, propagates real exceptions as
themselves, and abandons over-budget workers promptly. Heartbeat level: a
role (or inbox handler) that sleeps past the per-agent budget is abandoned
with a timeout event while the rest of the tick still runs; a normal agent
exception keeps taking the existing "agent_error" path.

Deterministic sim mode throughout. Wedged fakes block on an Event released
right after step() returns, so abandoned daemon workers exit immediately
instead of sleeping through the rest of the suite.
"""

from __future__ import annotations

import threading
import time
from typing import Any

import pytest

from sovereign.config import DebugConfig, EngineConfig
from sovereign.engine.heartbeat import TICK_METRICS_KEY, step
from sovereign.engine.watchdog import AgentTimeout, run_with_timeout
from sovereign.engine.world import bootstrap


def sim_world(tmp_path, **overrides):
    cfg = EngineConfig(mode="sim", data_dir=tmp_path, **overrides)  # type: ignore[arg-type]
    return bootstrap(cfg)


def events_of(world, kind: str) -> list[dict[str, Any]]:
    return [e for e in world.store.events(500) if e["kind"] == kind]


# ---------------------------------------------------------------------------
# run_with_timeout unit behavior


def test_fast_fn_value_is_returned():
    assert run_with_timeout(lambda: 41 + 1, seconds=5.0) == 42


def test_over_budget_fn_raises_agent_timeout_and_releases_caller_promptly():
    fired: list[bool] = []
    started = time.monotonic()
    with pytest.raises(AgentTimeout) as excinfo:
        run_with_timeout(
            lambda: time.sleep(2.0),
            seconds=0.2,
            agent="wedged",
            on_timeout=lambda: fired.append(True),
        )
    waited = time.monotonic() - started
    assert waited < 1.5  # released at the budget, not after the 2s sleep
    assert excinfo.value.agent == "wedged"
    assert excinfo.value.seconds == 0.2
    assert fired == [True]


def test_fn_exception_propagates_as_itself_not_agent_timeout():
    def boom() -> None:
        raise ValueError("real failure")

    with pytest.raises(ValueError, match="real failure"):
        run_with_timeout(boom, seconds=5.0)


def test_fast_failure_with_tiny_budget_is_not_masked_as_timeout():
    def boom() -> None:
        raise KeyError("missing")

    with pytest.raises(KeyError, match="missing"):
        run_with_timeout(boom, seconds=0.5)


@pytest.mark.parametrize("budget", [0.0, -1.0])
def test_nonpositive_seconds_runs_inline_without_limit(budget):
    threads: list[threading.Thread] = []

    def probe() -> str:
        threads.append(threading.current_thread())
        return "done"

    assert run_with_timeout(probe, seconds=budget) == "done"
    assert threads == [threading.current_thread()]


# ---------------------------------------------------------------------------
# heartbeat-level fault isolation


def test_wedged_role_is_abandoned_and_the_tick_still_completes(tmp_path, monkeypatch):
    world = sim_world(
        tmp_path,
        agent_timeout_seconds=0.2,
        debug=DebugConfig(enabled=True),
    )
    release = threading.Event()

    def scout(w):  # __name__ must match the pipeline role name
        release.wait(10.0)  # far beyond the 0.2s budget
        return []

    monkeypatch.setattr("sovereign.agents.roles.scout", scout)
    started = time.monotonic()
    report = step(world)
    elapsed = time.monotonic() - started
    release.set()  # let the abandoned worker exit now instead of sleeping on

    assert elapsed < 5.0  # the 10s wedge did not serialize into the tick

    timeouts = events_of(world, "agent_timeout")
    scout_timeouts = [e for e in timeouts if e["agent"] == "scout"]
    assert len(scout_timeouts) == 1
    assert scout_timeouts[0]["payload"] == {"agent": "scout", "seconds": 0.2}
    assert events_of(world, "agent_error") == []

    # The rest of the pipeline still ran this tick.
    kinds = {e["kind"] for e in world.store.events(500)}
    assert "mechanic" in kinds
    assert "snapshot" in kinds  # bookkeeper

    # Well-formed report; the timeout is counted like a tick error.
    assert report["tick"] == 1
    assert report["errors"] == len(timeouts) >= 1
    assert set(report) >= {
        "tick", "actions", "duration_ms", "errors", "comms_ms",
        "idle", "equity", "revenue", "trailing", "frozen", "pipeline",
    }
    ring = world.store.get_kv(TICK_METRICS_KEY)
    assert ring[-1]["errors"] == report["errors"]
    # The abandoned agent's elapsed is recorded as its full budget.
    assert ring[-1]["agents_ms"]["scout"] == 200.0

    # Modest escalation: slashed 3 points, nowhere near the freeze threshold.
    assert world.reputation.get("scout") == 67.0
    assert "scout" not in world.frozen
    assert events_of(world, "freeze") == []


def test_normal_agent_exception_still_takes_the_agent_error_path(tmp_path, monkeypatch):
    world = sim_world(tmp_path)  # default 30s budget: the wrapper is active

    def trader(w):
        raise RuntimeError("injected trader failure")

    monkeypatch.setattr("sovereign.agents.roles.trader", trader)
    report = step(world)

    errors = events_of(world, "agent_error")
    assert len(errors) == 1
    assert errors[0]["agent"] == "trader"
    assert errors[0]["payload"] == {"error": "injected trader failure"}
    assert events_of(world, "agent_timeout") == []
    assert events_of(world, "comms_timeout") == []
    assert report["errors"] == 1
    assert world.reputation.get("trader") == 65.0  # existing slash of 5, unchanged
    assert "trader" not in world.frozen


def test_wedged_inbox_handler_emits_comms_timeout_and_the_tick_continues(
    tmp_path, monkeypatch
):
    world = sim_world(tmp_path, agent_timeout_seconds=0.2)
    receipt = world.comms.send("director", "mechanic", "ping", {}, now=world.now)
    release = threading.Event()
    from sovereign.comms import handlers

    real_process_inbox = handlers.process_inbox

    def wedged(w, agent):
        if agent == "mechanic":
            release.wait(10.0)
            return []  # never touches the store after abandonment
        return real_process_inbox(w, agent)

    monkeypatch.setattr("sovereign.comms.handlers.process_inbox", wedged)
    report = step(world)
    release.set()

    comms_timeouts = events_of(world, "comms_timeout")
    assert [e["payload"] for e in comms_timeouts] == [{"agent": "mechanic"}]
    # The pump was abandoned, not failed: the message stays queued for a retry.
    assert world.store.get_message(receipt.message_ids[0])["status"] == "queued"

    # Mechanic itself still ran and was not punished for the comms wedge.
    kinds = {e["kind"] for e in world.store.events(500)}
    assert "mechanic" in kinds
    assert "snapshot" in kinds
    assert world.reputation.get("mechanic") == 70.0
    assert "mechanic" not in world.frozen

    # Comms timeouts are isolation events, not tick errors.
    assert events_of(world, "agent_timeout") == []
    assert events_of(world, "agent_error") == []
    assert report["errors"] == 0
