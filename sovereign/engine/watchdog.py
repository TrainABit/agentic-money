"""Wall-clock supervision for agent calls: one wedged agent must never
freeze the whole firm.

:func:`run_with_timeout` runs a callable on a daemon worker thread and joins
it for at most a budget of seconds. When the budget runs out the worker is
abandoned and :class:`AgentTimeout` is raised, so the heartbeat can move on
to the next agent instead of hanging the tick.

The honest guarantee: Python cannot force-kill a thread, so a timeout bounds
the *tick* (the caller stops waiting), never the wedged call itself. The
abandoned worker keeps running as a daemon thread in the background — a
thread stuck in a syscall lingers until the process restarts, and any side
effects the worker performs after abandonment still land (late, but the
store is thread-safe so they land consistently). Budgets protect the
liveness of the pipeline, not the resources of the stuck call.

Dependency-free by design: threading plus a result/exception box.
"""

from __future__ import annotations

import threading
from collections.abc import Callable
from typing import Any, TypeVar

__all__ = ["AgentTimeout", "run_with_timeout"]

T = TypeVar("T")


class AgentTimeout(Exception):
    """A supervised call exceeded its wall-clock budget.

    Carries ``agent`` (the label the caller attached; empty string when the
    caller supplied none) and ``seconds`` (the budget that was exceeded).
    """

    def __init__(self, agent: str, seconds: float) -> None:
        super().__init__(
            f"agent {agent or '<unnamed>'} exceeded its {seconds:g}s wall-clock budget"
        )
        self.agent = agent
        self.seconds = seconds


def run_with_timeout(
    fn: Callable[[], T],
    *,
    seconds: float,
    agent: str = "",
    on_timeout: Callable[[], None] | None = None,
) -> T:
    """Run ``fn`` with at most ``seconds`` of wall-clock time.

    ``fn`` executes on a daemon worker thread while the caller joins it for
    up to ``seconds``:

    - If it finishes in time, its return value is handed back. If it raised,
      that original exception is re-raised in the caller — a failing ``fn``
      is never masked as a timeout.
    - If the budget is exhausted first, ``on_timeout`` (when given) is called
      in the caller's thread and :class:`AgentTimeout` is raised carrying
      ``agent`` and ``seconds``.
    - ``seconds <= 0`` means no limit: ``fn`` runs inline on the caller's
      thread with no worker at all.

    Threads cannot be killed in Python, so on timeout the worker is simply
    abandoned: it stays alive (daemonized, so it never blocks interpreter
    exit) and may still complete — and apply side effects — later. This
    bounds the tick, not the wedged thread; a truly stuck syscall thread
    lingers until the process restarts. That is the honest guarantee.
    """
    if seconds <= 0:
        return fn()

    box: dict[str, Any] = {}

    def _worker() -> None:
        try:
            box["value"] = fn()
        except BaseException as exc:  # noqa: BLE001 - boxed and re-raised in the caller
            box["error"] = exc

    worker = threading.Thread(
        target=_worker, name=f"watchdog-{agent or 'fn'}", daemon=True
    )
    worker.start()
    worker.join(seconds)
    if worker.is_alive():
        if on_timeout is not None:
            on_timeout()
        raise AgentTimeout(agent, seconds)
    if "error" in box:
        raise box["error"]
    return box["value"]
