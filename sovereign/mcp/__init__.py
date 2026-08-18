"""Model Context Protocol client bridge.

External MCP servers (design/image generation, web search/research, code
hosting, payments, data) become money-making tools behind the engine's
existing safety model: fail-closed config, per-agent allowlists, untrusted-
data fencing, sanitized errors, and vault-resolved credentials. The optional
`mcp` SDK is imported only inside build_transport, so this package imports
cleanly when only ".[dev]" is installed.
"""

from sovereign.mcp.client import McpClient
from sovereign.mcp.fakes import FakeTransport
from sovereign.mcp.registry import McpRegistry
from sovereign.mcp.transport import (
    McpResult,
    McpToolSpec,
    McpTransport,
    build_transport,
)

__all__ = [
    "FakeTransport",
    "McpClient",
    "McpRegistry",
    "McpResult",
    "McpToolSpec",
    "McpTransport",
    "build_transport",
]
