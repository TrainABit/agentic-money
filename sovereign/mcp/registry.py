"""Lazy fleet of MCP clients behind one fail-closed switch.

Nothing connects at construction: the first tools()/tools_for()/call() builds
a transport through the public, injectable `transport_factory` (default: the
real SDK transport from build_transport). A server whose connect fails is
recorded in errors() — exception class name only — and skipped until close()
resets the registry; a discover failure is recorded but the client is kept so
a transient outage can recover. Disabled config short-circuits everything
without ever touching a transport.
"""

from __future__ import annotations

from collections.abc import Callable, Iterable

from sovereign.config import McpConfig, McpServerConfig
from sovereign.mcp.client import McpClient
from sovereign.mcp.transport import (
    McpResult,
    McpToolSpec,
    McpTransport,
    build_transport,
)


class McpRegistry:
    def __init__(
        self,
        config: McpConfig,
        *,
        secret_resolver: Callable[[str], str],
        transport_factory: Callable[..., McpTransport] | None = None,
    ) -> None:
        self._config = config
        self._secret_resolver = secret_resolver
        # Public so tests (and sims) inject FakeTransport factories.
        self.transport_factory = transport_factory or build_transport
        self.enabled = bool(config.enabled)
        self._clients: dict[str, McpClient] = {}
        self._failed: set[str] = set()
        self._errors: list[dict] = []

    def servers(self) -> list[str]:
        return [server.name for server in self._config.servers]

    def tools(self) -> list[McpToolSpec]:
        if not self.enabled:
            return []
        return self._discover(self._config.servers)

    def tools_for(self, agent: str) -> list[McpToolSpec]:
        if not self.enabled:
            return []
        permitted = [
            server for server in self._config.servers if agent in server.allow_agents
        ]
        return self._discover(permitted)

    def call(self, agent: str, server: str, tool: str, arguments: dict) -> McpResult:
        if not self.enabled:
            return McpResult(ok=False, error="mcp disabled")
        if self._server_config(server) is None:
            return McpResult(ok=False, error="unknown server")
        client = self._client(server)
        if client is None:
            return McpResult(ok=False, error="server unavailable")
        return client.call(agent, tool, arguments)

    def errors(self) -> list[dict]:
        return [dict(entry) for entry in self._errors]

    def close(self) -> None:
        """Close every built client and clear the cache; idempotent."""
        clients = list(self._clients.values())
        self._clients.clear()
        self._failed.clear()
        for client in clients:
            try:
                client.close()
            except Exception:
                pass

    # -- internals -------------------------------------------------------------

    def _server_config(self, name: str) -> McpServerConfig | None:
        for server in self._config.servers:
            if server.name == name:
                return server
        return None

    def _client(self, name: str) -> McpClient | None:
        if name in self._failed:
            return None
        cached = self._clients.get(name)
        if cached is not None:
            return cached
        server = self._server_config(name)
        if server is None:
            return None
        try:
            transport = self.transport_factory(
                server, secret_resolver=self._secret_resolver
            )
        except Exception as exc:  # noqa: BLE001 - recorded, server skipped
            self._failed.add(name)
            self._record_error(name, "connect", exc)
            return None
        client = McpClient(server, transport)
        self._clients[name] = client
        return client

    def _discover(self, servers: Iterable[McpServerConfig]) -> list[McpToolSpec]:
        specs: list[McpToolSpec] = []
        for server in servers:
            client = self._client(server.name)
            if client is None:
                continue
            try:
                specs.extend(client.discover())
            except Exception as exc:  # noqa: BLE001 - tolerate per-server failure
                self._record_error(server.name, "discover", exc)
        return specs

    def _record_error(self, server: str, op: str, exc: BaseException) -> None:
        # Class name only — connect/discover messages may echo commands or env.
        entry = {"server": server, "op": op, "error": type(exc).__name__}
        if entry not in self._errors:
            self._errors.append(entry)
