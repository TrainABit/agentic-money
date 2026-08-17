from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Callable, TYPE_CHECKING

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

    def allows(self, agent: str) -> bool:
        return "*" in self.allow or agent in self.allow


class Registry:
    def __init__(self) -> None:
        self._tools: dict[str, Tool] = {}
        self.world: World | None = None

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
        try:
            data = tool.fn(world, **kwargs)
            world.store.emit("tool", {"tool": name, "ok": True}, caller)
            return ToolResult(True, data=data)
        except Exception as e:
            world.store.emit("tool", {"tool": name, "ok": False, "error": str(e)[:240]}, caller)
            errs = dict(world.store.get_kv("tool_errors") or {})
            errs[name] = int(errs.get(name, 0)) + 1
            world.store.set_kv("tool_errors", errs)
            return ToolResult(False, error=str(e)[:240])
