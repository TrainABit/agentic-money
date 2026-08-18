"""Optional multi-process agent waves.

Off by default. When enabled, agents not listed in ``workers.in_process``
(and not mechanic) run in spawned processes that reopen the same SQLite
world. Workers never call ``start_tick`` / ``finish_tick`` and never write
the parent ``meta`` blob — they return a patch the supervisor merges.

Hunter still runs before closer, and closer before crafter: those roles
stay in successive waves.
"""

from __future__ import annotations

from concurrent.futures import ProcessPoolExecutor, TimeoutError as FuturesTimeout
from multiprocessing import get_context
from typing import Any, Callable

from sovereign.config import EngineConfig


PIPELINE_NAMES: tuple[str, ...] = (
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
)

WAVES: tuple[tuple[str, ...], ...] = (
    ("mechanic",),
    ("bookkeeper",),
    ("risk", "ethics"),
    ("director",),
    ("hunter",),
    ("closer",),
    ("crafter",),
    ("trader",),
    ("publisher", "scout", "operator"),
    ("treasurer", "auditor", "improver"),
    ("courier",),
)

ALWAYS_IN_PROCESS = frozenset({"mechanic"})
BROKER_AGENTS = frozenset({"bookkeeper", "risk", "trader"})

assert tuple(name for wave in WAVES for name in wave) == PIPELINE_NAMES
assert PIPELINE_NAMES.index("hunter") < PIPELINE_NAMES.index("closer")
assert PIPELINE_NAMES.index("closer") < PIPELINE_NAMES.index("crafter")


def stays_in_process(name: str, config: EngineConfig) -> bool:
    if name in ALWAYS_IN_PROCESS:
        return True
    return name in set(config.workers.in_process)


def run_agent_job(payload: dict[str, Any]) -> dict[str, Any]:
    """Child-process entry. Importable at spawn. Does not pickle World."""
    from sovereign.engine.heartbeat import run_one_agent
    from sovereign.engine.world import bootstrap

    agent = str(payload.get("agent") or "")
    try:
        config = EngineConfig.model_validate(payload["config"])
        world = bootstrap(config, heal=False)
        try:
            queued = (
                world.store.queued_recipient_counts() if world.comms is not None else {}
            )
            tick = run_one_agent(
                world,
                agent,
                queued=queued,
                timeout_s=config.agent_timeout_seconds,
                tracing=False,
            )
            patch = {
                "ok": tick.errors == 0,
                "agent": agent,
                "tick": world.tick,
                "errors": tick.errors,
                "ms": tick.ms,
                "actions": len(tick.actions),
                "broker": world.broker.snapshot(),
                "frozen": sorted(world.frozen),
                "freeze_info": world.freeze_info,
                "freeze_since": world.freeze_since,
                "last_prices": world.last_prices,
                "reputation": dict(world.reputation.scores),
                "timeout": tick.timeout,
                "error": tick.error,
            }
            return patch
        finally:
            try:
                world.store.close()
            except Exception:
                pass
    except Exception as exc:
        return {
            "ok": False,
            "agent": agent,
            "error": f"{type(exc).__name__}: {exc}",
            "errors": 1,
        }


def apply_worker_patches(world: Any, patches: list[dict[str, Any]]) -> None:
    """Merge child patches in wave order. Broker only from broker-owning roles."""
    frozen = set(world.frozen)
    freeze_info = dict(world.freeze_info)
    freeze_since = dict(world.freeze_since)
    broker_snap = None
    for patch in patches:
        if not patch:
            continue
        agent = str(patch.get("agent") or "")
        if patch.get("broker") and agent in BROKER_AGENTS:
            broker_snap = patch["broker"]
        if patch.get("frozen") is not None:
            frozen |= set(patch["frozen"])
            freeze_info.update(patch.get("freeze_info") or {})
            freeze_since.update(
                {k: int(v) for k, v in (patch.get("freeze_since") or {}).items()}
            )
        if patch.get("last_prices") and agent in {"trader", "mechanic"}:
            world.last_prices = dict(patch["last_prices"])
        if patch.get("reputation"):
            world.reputation.scores.update(
                {k: float(v) for k, v in patch["reputation"].items()}
            )
    if broker_snap:
        world.broker.restore(broker_snap, now=world.now)
    world.frozen = frozen
    world.freeze_info = freeze_info
    world.freeze_since = freeze_since
    world.ledger._bal_cache = None


class WorkerPool:
    def __init__(self, config: EngineConfig) -> None:
        self.config = config
        self._pool: ProcessPoolExecutor | None = None

    def __enter__(self) -> "WorkerPool":
        return self

    def _ensure_pool(self) -> ProcessPoolExecutor | None:
        procs = max(0, int(self.config.workers.max_procs))
        if procs <= 0:
            return None
        if self._pool is None:
            self._pool = ProcessPoolExecutor(
                max_workers=procs, mp_context=get_context("spawn")
            )
        return self._pool

    def __exit__(self, *exc: object) -> None:
        if self._pool is not None:
            self._pool.shutdown(wait=True, cancel_futures=True)
            self._pool = None

    def run_wave(
        self,
        names: tuple[str, ...],
        world: Any,
        run_one: Callable[[str], Any],
    ) -> list[dict[str, Any]]:
        in_proc = [name for name in names if stays_in_process(name, world.config)]
        remote = [name for name in names if name not in in_proc]
        results: list[dict[str, Any]] = []

        for name in in_proc:
            tick = run_one(name)
            results.append({"agent": name, "in_process": True, "ok": tick.errors == 0})

        if not remote:
            return results

        world.persist_kv()
        payload_config = world.config.model_dump(mode="json")
        timeout_s = float(world.config.agent_timeout_seconds) + 30.0
        pool = self._ensure_pool()
        if pool is None:
            for name in remote:
                results.append(
                    run_agent_job({"config": payload_config, "agent": name})
                )
        else:
            futs = [
                (
                    name,
                    pool.submit(
                        run_agent_job, {"config": payload_config, "agent": name}
                    ),
                )
                for name in remote
            ]
            for name, fut in futs:
                try:
                    results.append(fut.result(timeout=timeout_s))
                except FuturesTimeout:
                    results.append(
                        {
                            "ok": False,
                            "agent": name,
                            "error": f"timeout after {timeout_s:g}s",
                            "timeout": True,
                            "errors": 1,
                        }
                    )
        apply_worker_patches(
            world, [row for row in results if row.get("agent") in remote]
        )
        world.persist_kv()
        return results


def run_standalone_agent(config: EngineConfig, agent: str) -> dict[str, Any]:
    """One-shot worker used by ``sovereign worker``. Does not take engine.lock."""
    from sovereign.engine.heartbeat import run_one_agent
    from sovereign.engine.world import bootstrap

    world = bootstrap(config, heal=False)
    queued = world.store.queued_recipient_counts() if world.comms is not None else {}
    tick = run_one_agent(
        world,
        agent,
        queued=queued,
        timeout_s=config.agent_timeout_seconds,
        tracing=False,
    )
    world.persist_kv()
    world.store.close()
    return {
        "ok": tick.errors == 0,
        "agent": agent,
        "actions": len(tick.actions),
        "errors": tick.errors,
        "ms": tick.ms,
        "timeout": tick.timeout,
        "error": tick.error,
    }


def dump_pipeline() -> dict[str, Any]:
    return {
        "pipeline": list(PIPELINE_NAMES),
        "waves": [list(wave) for wave in WAVES],
        "always_in_process": sorted(ALWAYS_IN_PROCESS),
    }
