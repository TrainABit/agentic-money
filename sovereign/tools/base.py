from __future__ import annotations

import time
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any, Callable

if TYPE_CHECKING:
    from sovereign.engine.world import World


@dataclass
class ToolResult:
    ok: bool
    data: Any = None
    error: str | None = None

    def as_dict(self) -> dict[str, Any]:
        return {"ok": self.ok, "data": self.data, "error": self.error}


@dataclass
class Tool:
    name: str
    description: str
    allow: frozenset[str]
    fn: Callable[..., Any]
    wants_caller: bool = False

    def allows(self, agent: str) -> bool:
        return "*" in self.allow or agent in self.allow


class Registry:
    def __init__(self) -> None:
        self._tools: dict[str, Tool] = {}
        self.world: World | None = None
        # In-memory per-tool call accounting; never persisted.
        self.stats: dict[str, dict[str, float]] = {}

    def bind(self, world: World) -> None:
        self.world = world

    def register(self, tool: Tool) -> None:
        self._tools[tool.name] = tool

    def names(self) -> list[str]:
        return sorted(self._tools)

    def available_to(self, agent: str) -> list[str]:
        return sorted(t.name for t in self._tools.values() if t.allows(agent))

    def manifest(self) -> dict[str, dict[str, Any]]:
        return {
            name: {"description": t.description, "allow": sorted(t.allow)}
            for name, t in sorted(self._tools.items())
        }

    def call(self, caller: str, name: str, /, **kwargs: Any) -> ToolResult:
        world = self.world
        if world is None:
            return ToolResult(False, error="registry unbound")
        tool = self._tools.get(name)
        if not tool:
            return ToolResult(False, error=f"unknown tool {name}")
        if not tool.allows(caller):
            world.store.emit("tool_denied", {"tool": name}, caller)
            return ToolResult(False, error=f"denied: {caller} cannot use {name}")
        started = time.monotonic()
        try:
            if tool.wants_caller:
                # The authenticated caller always wins: a "caller" kwarg smuggled
                # in by the caller is discarded, never forwarded.
                kwargs.pop("caller", None)
                data = tool.fn(world, caller=caller, **kwargs)
            else:
                data = tool.fn(world, **kwargs)
            ms = (time.monotonic() - started) * 1000.0
            self._observe(world, caller, name, ms, True, None)
            world.store.emit("tool", {"tool": name, "ok": True}, caller)
            return ToolResult(True, data=data)
        except Exception as e:
            ms = (time.monotonic() - started) * 1000.0
            error = str(e)[:240]
            self._observe(world, caller, name, ms, False, error)
            world.store.emit("tool", {"tool": name, "ok": False, "error": error}, caller)
            errs = dict(world.store.get_kv("tool_errors") or {})
            errs[name] = int(errs.get(name, 0)) + 1
            world.store.set_kv("tool_errors", errs)
            return ToolResult(False, error=error)

    def _observe(
        self, world: World, caller: str, name: str, ms: float, ok: bool, error: str | None
    ) -> None:
        """Account one executed call: stats, slow-call event, debug trace."""
        entry = self.stats.setdefault(
            name, {"calls": 0, "errors": 0, "total_ms": 0.0, "max_ms": 0.0}
        )
        entry["calls"] = int(entry["calls"]) + 1
        if not ok:
            entry["errors"] = int(entry["errors"]) + 1
        entry["total_ms"] = float(entry["total_ms"]) + ms
        entry["max_ms"] = max(float(entry["max_ms"]), ms)
        # Slow-call events carry the tool name and duration only — never
        # kwargs or payloads.
        if ms > float(world.config.debug.slow_tool_ms):
            world.store.emit("tool_slow", {"tool": name, "ms": round(ms, 1)}, caller)
        collector = getattr(world, "debug_trace", None)
        if collector is not None:
            collector.record_tool(caller, name, ms, ok, error=error)

    def stats_snapshot(self) -> dict[str, dict[str, float]]:
        """Sanitized copy of the per-tool stats, with a derived avg_ms."""
        snapshot: dict[str, dict[str, float]] = {}
        for name, entry in sorted(self.stats.items()):
            calls = int(entry["calls"])
            total_ms = float(entry["total_ms"])
            snapshot[name] = {
                "calls": calls,
                "errors": int(entry["errors"]),
                "total_ms": round(total_ms, 3),
                "max_ms": round(float(entry["max_ms"]), 3),
                "avg_ms": round(total_ms / calls, 3) if calls else 0.0,
            }
        return snapshot
