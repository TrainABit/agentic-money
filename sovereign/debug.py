"""Opt-in per-tick execution traces for debugging slow or failing ticks.

:class:`TraceCollector` buffers tool, agent, and comms timings during a tick
and flushes them to one JSONL file per tick (summary first, then one line per
event). Disabled collectors are strict no-ops: no buffering and zero
filesystem writes, so the collector can stay wired into every world.

Traceback tails are stored only in trace files, never in store events, and
are truncated to :data:`TRACEBACK_TAIL_CHARS` characters.
"""

from __future__ import annotations

import json
import os
from collections.abc import Mapping
from pathlib import Path
from typing import Any

from sovereign.config import DebugConfig

__all__ = ["TRACEBACK_TAIL_CHARS", "TraceCollector"]

TRACEBACK_TAIL_CHARS = 800
_TRACE_GLOB = "trace_tick_*.jsonl"


class TraceCollector:
    """Buffers one tick's debug events and writes them as a JSONL trace file."""

    def __init__(
        self,
        trace_dir: Path,
        config: DebugConfig,
        *,
        env: Mapping[str, str] | None = None,
    ) -> None:
        self.trace_dir = Path(trace_dir)
        self.config = config
        self._env: Mapping[str, str] = os.environ if env is None else env
        self._tick: int | None = None
        self._started_iso: str | None = None
        self._events: list[dict[str, Any]] = []

    @property
    def enabled(self) -> bool:
        return bool(self.config.enabled) or self._env.get("SOVEREIGN_DEBUG") == "1"

    def begin_tick(self, tick: int, now_iso: str) -> None:
        if not self.enabled:
            return
        self._tick = int(tick)
        self._started_iso = str(now_iso)
        self._events = []

    def record_tool(
        self, caller: str, tool: str, ms: float, ok: bool, error: str | None = None
    ) -> None:
        if not self.enabled:
            return
        self._events.append(
            {
                "event": "tool",
                "caller": str(caller),
                "tool": str(tool),
                "ms": round(float(ms), 3),
                "ok": bool(ok),
                "error": error,
            }
        )

    def record_agent(
        self,
        agent: str,
        ms: float,
        actions: int,
        error: str | None = None,
        traceback_tail: str | None = None,
    ) -> None:
        if not self.enabled:
            return
        tail = None
        if traceback_tail is not None and self.config.include_tracebacks:
            tail = str(traceback_tail)[-TRACEBACK_TAIL_CHARS:]
        self._events.append(
            {
                "event": "agent",
                "agent": str(agent),
                "ms": round(float(ms), 3),
                "actions": int(actions),
                "error": error,
                "traceback_tail": tail,
            }
        )

    def record_comms(self, processed: int, expired: int, ms: float) -> None:
        if not self.enabled:
            return
        self._events.append(
            {
                "event": "comms",
                "processed": int(processed),
                "expired": int(expired),
                "ms": round(float(ms), 3),
            }
        )

    def end_tick(self, summary: dict[str, Any]) -> Path | None:
        if not self.enabled:
            self._tick = None
            self._started_iso = None
            self._events = []
            return None
        tick = int(summary.get("tick", self._tick or 0))
        merged: dict[str, Any] = dict(summary)
        if self._started_iso is not None:
            merged.setdefault("started_ts", self._started_iso)
        self.trace_dir.mkdir(parents=True, exist_ok=True)
        path = self.trace_dir / f"trace_tick_{tick:08d}.jsonl"
        lines = [json.dumps(merged, default=str)]
        lines.extend(json.dumps(event, default=str) for event in self._events)
        path.write_text("\n".join(lines) + "\n")
        self._tick = None
        self._started_iso = None
        self._events = []
        self._enforce_retention()
        return path

    def _enforce_retention(self) -> None:
        keep = max(1, int(self.config.trace_retention_files))
        files = sorted(self.trace_dir.glob(_TRACE_GLOB))
        for stale in files[:-keep]:
            try:
                stale.unlink()
            except OSError:
                pass

    def latest(self, n: int = 1) -> list[Path]:
        """The newest trace files, newest first."""
        if not self.trace_dir.exists():
            return []
        files = sorted(self.trace_dir.glob(_TRACE_GLOB), reverse=True)
        return files[: max(0, int(n))]

    @staticmethod
    def read_summary(path: Path) -> dict[str, Any]:
        """The first (summary) line of one trace file."""
        with Path(path).open() as handle:
            first = handle.readline()
        loaded = json.loads(first) if first.strip() else {}
        return loaded if isinstance(loaded, dict) else {}
