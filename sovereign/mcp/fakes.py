"""Deterministic in-memory `McpTransport` for unit tests.

No `mcp` import, no network, no threads. `calls` records every
(tool, arguments) pair so tests can assert routing; close() flips `closed`
and, when provided, the shared `closed_flag` (an Event-like object with
.set(), a dict given a "closed" key, or a list appended with the transport).
"""

from __future__ import annotations

import json
from collections.abc import Callable
from typing import Any

from sovereign.mcp.transport import McpResult, McpToolSpec, McpTransport


class FakeTransport(McpTransport):
    def __init__(
        self,
        tools: list[McpToolSpec],
        handler: Callable[[str, dict], McpResult] | None = None,
        *,
        closed_flag: Any = None,
    ) -> None:
        self.tools = list(tools)
        self.handler = handler
        self.calls: list[tuple[str, dict]] = []
        self.closed = False
        self._closed_flag = closed_flag

    def list_tools(self) -> list[McpToolSpec]:
        return list(self.tools)

    def call_tool(self, name: str, arguments: dict, *, timeout: float) -> McpResult:
        self.calls.append((name, dict(arguments)))
        if self.handler is not None:
            return self.handler(name, arguments)
        return McpResult(ok=True, text=json.dumps(arguments)[:200])

    def close(self) -> None:
        self.closed = True
        flag = self._closed_flag
        if flag is None:
            return
        if hasattr(flag, "set"):
            flag.set()
        elif isinstance(flag, dict):
            flag["closed"] = True
        elif isinstance(flag, list):
            flag.append(self)
