"""Bounded sim soak: hundreds of heartbeat ticks with periodic health gates.

The slow mark is informational only; the test is collected in the default
suite on purpose (no marker filter needed). TICKS is capped so the whole
soak stays around 15 seconds.
"""

from __future__ import annotations

import pytest

from sovereign.capital.invariants import verify_invariants
from sovereign.config import EngineConfig
from sovereign.engine.heartbeat import step
from sovereign.engine.world import bootstrap

TICKS = 250
CHECK_EVERY = 50
# emit() prunes to the retention cap on every insert, so the live row count
# can only sit at the cap; the margin just keeps the gate honest.
EVENT_MARGIN = 100
# The bus stays near-empty in sim (queued inboxes are pumped every tick and
# settled rows are pruned on a 50-tick cadence); these caps catch runaway
# growth without asserting an exact shape the engine does not promise.
COMMS_QUEUED_CAP = 200
COMMS_TOTAL_CAP = 5_000


@pytest.mark.slow
def test_soak_sim_stays_invariant_clean_and_bounded(tmp_path):
    config = EngineConfig(mode="sim", data_dir=tmp_path)  # type: ignore[arg-type]
    world = bootstrap(config)
    retention = world.store.event_retention
    assert retention is not None and retention > 0

    last_labor = 0.0
    for tick in range(1, TICKS + 1):
        step(world)  # any exception here fails the soak
        if tick % CHECK_EVERY:
            continue

        report = verify_invariants(world)
        assert report["ok"] is True, report["checks"]

        # Full-table count (store.events() is limit-capped); the soak is
        # single-threaded so reading through the store connection is safe.
        events = int(
            world.store.conn.execute("SELECT COUNT(*) FROM events").fetchone()[0]
        )
        assert events <= retention + EVENT_MARGIN

        counts = world.comms.counts()
        assert counts.get("queued", 0) <= COMMS_QUEUED_CAP, counts
        assert sum(counts.values()) <= COMMS_TOTAL_CAP, counts

        # Labor revenue only ever grows in sim: monotonic across checkpoints.
        labor = float(world.ledger.snapshot(now=world.now)["labor_usd"])
        assert labor >= last_labor
        last_labor = labor

    assert world.tick >= TICKS
    assert last_labor >= 0.0
