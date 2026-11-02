"""Shared MCP bits: Cairnward's error type and the message size limit.

The protocol itself is the official MCP Python SDK's (ADR-0015 amendment): upstream
servers in `mcp.upstream`, the client-facing endpoint in `mcp.http`.
"""

from __future__ import annotations

MAX_MESSAGE = 16 * 1024 * 1024


class McpError(Exception):
    """The server answered with a JSON-RPC error, broke the protocol, or went away."""

    def __init__(self, message: str, code: int | None = None):
        super().__init__(message)
        self.code = code
