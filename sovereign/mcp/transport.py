"""MCP transport types, protocol, and the real SDK-backed transport.

`McpToolSpec`/`McpResult`/`McpTransport` are plain dependency-free types used
across the package. `build_transport` is the ONLY place the optional `mcp`
SDK is imported (lazily, inside the function), so this module — and everything
that imports it — stays importable when only ".[dev]" is installed. Unit tests
use `sovereign.mcp.fakes.FakeTransport` instead.

The official SDK is asyncio-based; each real transport owns one dedicated,
persistent event-loop thread. The connection (stdio subprocess or streamable
HTTP) and its ClientSession are entered once on that loop, kept open across
calls, and exited in close(). The sync list_tools/call_tool facade submits
coroutines to the loop and waits with a bounded timeout. Tool arguments and
resolved env secrets are never logged, and connect failures surface as the
exception class name only (raw messages could echo command lines or env).

Works against SDK v1.9+ and v2: result/tool fields are read under both the
camelCase (v1) and snake_case (v2) spellings, and the streamable-HTTP client
is imported under its new name with a fallback to the removed v1 alias.
"""

from __future__ import annotations

import asyncio
import os
import threading
from collections.abc import Callable
from contextlib import asynccontextmanager
from dataclasses import dataclass
from typing import Any, Protocol

from sovereign.config import McpServerConfig

TEXT_CAP = 10_000  # hard cap for normalized tool-output text
_CONNECT_GRACE_S = 10.0
_CLOSE_GRACE_S = 5.0


@dataclass(frozen=True)
class McpToolSpec:
    """One discovered tool on one server; every field is untrusted metadata."""

    server: str
    name: str
    description: str
    input_schema: dict


@dataclass
class McpResult:
    ok: bool
    text: str = ""
    data: Any = None
    error: str | None = None


class McpTransport(Protocol):
    def list_tools(self) -> list[McpToolSpec]: ...

    def call_tool(self, name: str, arguments: dict, *, timeout: float) -> McpResult: ...

    def close(self) -> None: ...


def build_transport(
    server: McpServerConfig, *, secret_resolver: Callable[[str], str]
) -> McpTransport:
    """Real SDK transport for *server*; requires the optional [mcp] extra.

    The `mcp` import lives here — not at module scope — so environments
    installed with only ".[dev]" can import and unit-test everything else.
    """
    try:
        import mcp  # noqa: F401  (deferred: the [mcp] extra may be absent)
    except ImportError as exc:
        raise RuntimeError(
            "MCP integration requires the [mcp] extra: pip install -e '.[mcp]'"
        ) from exc
    return _SdkTransport(server, secret_resolver)


def _first_attr(obj: Any, *names: str, default: Any = None) -> Any:
    """First non-None attribute among *names* (v2 snake_case, v1 camelCase)."""
    for name in names:
        value = getattr(obj, name, None)
        if value is not None:
            return value
    return default


def _content_text(result: Any) -> str:
    """Concatenate text content parts, capped at TEXT_CAP characters."""
    parts: list[str] = []
    for part in getattr(result, "content", None) or ():
        text = getattr(part, "text", None)
        if isinstance(text, str):
            parts.append(text)
    return "\n".join(parts)[:TEXT_CAP]


class _SdkTransport:
    """Sync facade over the asyncio SDK: one persistent loop thread, one
    long-lived session, entered on first use and torn down in close()."""

    def __init__(
        self, server: McpServerConfig, secret_resolver: Callable[[str], str]
    ) -> None:
        self._server = server
        self._secret_resolver = secret_resolver
        self._lock = threading.Lock()
        self._loop: asyncio.AbstractEventLoop | None = None
        self._thread: threading.Thread | None = None
        self._session: Any = None
        self._stop_session: asyncio.Event | None = None
        self._session_future: Any = None
        self._connect_error: BaseException | None = None
        self._closed = False

    # -- public sync facade ---------------------------------------------------

    def list_tools(self) -> list[McpToolSpec]:
        session = self._ensure_session()
        listed = self._submit(session.list_tools(), self._server.timeout_s)
        specs: list[McpToolSpec] = []
        for tool in getattr(listed, "tools", None) or ():
            name = str(getattr(tool, "name", "") or "")
            if not name:
                continue
            schema = _first_attr(tool, "input_schema", "inputSchema", default={})
            specs.append(
                McpToolSpec(
                    server=self._server.name,
                    name=name,
                    description=str(getattr(tool, "description", None) or ""),
                    input_schema=dict(schema) if isinstance(schema, dict) else {},
                )
            )
        return specs

    def call_tool(self, name: str, arguments: dict, *, timeout: float) -> McpResult:
        session = self._ensure_session()
        result = self._submit(session.call_tool(str(name), dict(arguments)), timeout)
        is_error = bool(_first_attr(result, "is_error", "isError", default=False))
        return McpResult(
            ok=not is_error,
            text=_content_text(result),
            data=_first_attr(result, "structured_content", "structuredContent"),
            error="tool reported an error" if is_error else None,
        )

    def close(self) -> None:
        with self._lock:
            if self._closed:
                return
            self._closed = True
            self._teardown_locked()

    # -- event-loop plumbing ---------------------------------------------------

    def _ensure_session(self) -> Any:
        with self._lock:
            if self._closed:
                raise RuntimeError("transport is closed")
            if self._session is not None:
                return self._session
            loop = asyncio.new_event_loop()
            thread = threading.Thread(
                target=loop.run_forever,
                name=f"mcp-{self._server.name}",
                daemon=True,
            )
            thread.start()
            self._loop, self._thread = loop, thread
            self._connect_error = None
            ready = threading.Event()
            self._session_future = asyncio.run_coroutine_threadsafe(
                self._session_task(ready), loop
            )
            deadline = max(float(self._server.timeout_s), 1.0) + _CONNECT_GRACE_S
            if not ready.wait(deadline):
                self._teardown_locked()
                raise RuntimeError("MCP connect timed out")
            if self._connect_error is not None:
                error = self._connect_error
                self._teardown_locked()
                # Class name only: connect errors may echo command lines/env.
                raise RuntimeError(
                    f"MCP connect failed: {type(error).__name__}"
                ) from error
            return self._session

    async def _session_task(self, ready: threading.Event) -> None:
        try:
            async with self._connected() as session:
                self._session = session
                self._stop_session = asyncio.Event()
                ready.set()
                await self._stop_session.wait()
        except BaseException as exc:  # noqa: BLE001 - reported via _connect_error
            self._connect_error = exc
        finally:
            ready.set()

    @asynccontextmanager
    async def _connected(self):
        from mcp import ClientSession

        if self._server.transport == "http":
            try:  # SDK v2 name; v1.9+ ships it alongside the old alias
                from mcp.client.streamable_http import (
                    streamable_http_client as connect_http,
                )
            except ImportError:  # very old v1 spelling (removed in v2)
                from mcp.client.streamable_http import (
                    streamablehttp_client as connect_http,
                )
            async with connect_http(str(self._server.url or "")) as streams:
                read, write = streams[0], streams[1]  # v1 yields a 3rd callback
                async with ClientSession(read, write) as session:
                    await session.initialize()
                    yield session
        else:
            from mcp import StdioServerParameters

            try:
                from mcp.client.stdio import stdio_client
            except ImportError:  # v2 re-export location
                from mcp import stdio_client

            env = dict(os.environ)
            for var, ref in self._server.env_credentials.items():
                env[str(var)] = str(self._secret_resolver(ref))
            params = StdioServerParameters(
                command=str(self._server.command or ""),
                args=list(self._server.args),
                env=env,
            )
            async with stdio_client(params) as (read, write):
                async with ClientSession(read, write) as session:
                    await session.initialize()
                    yield session

    def _submit(self, coro: Any, timeout: float) -> Any:
        loop = self._loop
        if loop is None:
            raise RuntimeError("transport is closed")
        bounded = max(float(timeout), 0.1)
        future = asyncio.run_coroutine_threadsafe(
            asyncio.wait_for(coro, bounded), loop
        )
        try:
            return future.result(bounded + _CONNECT_GRACE_S)
        except BaseException:
            future.cancel()
            raise

    def _teardown_locked(self) -> None:
        """Stop the session task, the loop, and the thread; never raises."""
        loop, thread = self._loop, self._thread
        stop_event, session_future = self._stop_session, self._session_future
        self._loop = self._thread = None
        self._session = None
        self._stop_session = None
        self._session_future = None
        self._connect_error = None
        if loop is None:
            return
        try:
            if stop_event is not None:
                loop.call_soon_threadsafe(stop_event.set)
            if session_future is not None:
                try:
                    session_future.result(timeout=_CLOSE_GRACE_S)
                except BaseException:  # noqa: BLE001 - teardown is best-effort
                    session_future.cancel()
            loop.call_soon_threadsafe(loop.stop)
            if thread is not None:
                thread.join(timeout=_CLOSE_GRACE_S)
            if not loop.is_running():
                loop.close()
        except Exception:
            pass
