"""A minimal MCP client over a byte stream (stdio of a server, or a socket bridged to it).

MCP's stdio transport: newline-delimited JSON-RPC 2.0 messages, UTF-8, no embedded
newlines. Only what a tool proxy needs: initialize, tools/list (paged), tools/call, and
polite answers to requests the server sends us (ping, roots/list; anything else is
"method not found", so servers can't sample our models or ask the user through us).
"""

from __future__ import annotations

import asyncio
import contextlib
import json
from collections.abc import Callable
from typing import Any

from cairnward import __version__

CLIENT_PROTOCOL = "2025-06-18"
MAX_MESSAGE = 16 * 1024 * 1024


class McpError(Exception):
    """The server answered with a JSON-RPC error, broke the protocol, or went away."""

    def __init__(self, message: str, code: int | None = None):
        super().__init__(message)
        self.code = code


class Connection:
    def __init__(
        self,
        reader: asyncio.StreamReader,
        writer: asyncio.StreamWriter,
        *,
        on_notification: Callable[[str, dict[str, Any]], None] | None = None,
    ):
        self._reader = reader
        self._writer = writer
        self._next_id = 0
        self._waiting: dict[int, asyncio.Future[Any]] = {}
        self._on_notification = on_notification
        self._task = asyncio.create_task(self._read_loop())
        self.closed = asyncio.Event()
        self.server_info: dict[str, Any] = {}
        self.protocol_version = ""

    # ------------------------------------------------------------------ plumbing

    async def _send(self, message: dict[str, Any]) -> None:
        data = json.dumps(message, ensure_ascii=False, separators=(",", ":")).encode() + b"\n"
        self._writer.write(data)
        await self._writer.drain()

    async def _read_loop(self) -> None:
        try:
            while True:
                try:
                    line = await self._reader.readuntil(b"\n")
                except asyncio.IncompleteReadError:
                    break
                except asyncio.LimitOverrunError:
                    break  # a message over the limit: the stream can't be resynchronised
                if not line.strip():
                    continue
                try:
                    msg = json.loads(line)
                except ValueError:
                    continue  # servers sometimes print logs to stdout; ignore non-JSON lines
                if isinstance(msg, dict):
                    await self._dispatch(msg)
        finally:
            self.closed.set()
            for fut in self._waiting.values():
                if not fut.done():
                    fut.set_exception(McpError("the MCP server closed the connection"))
            self._waiting.clear()

    async def _dispatch(self, msg: dict[str, Any]) -> None:
        if "method" in msg and "id" in msg:  # a request from the server
            method = msg["method"]
            if method == "ping":
                await self._send({"jsonrpc": "2.0", "id": msg["id"], "result": {}})
            elif method == "roots/list":
                await self._send({"jsonrpc": "2.0", "id": msg["id"], "result": {"roots": []}})
            else:
                error = {"code": -32601, "message": f"{method} is not offered by Cairnward"}
                await self._send({"jsonrpc": "2.0", "id": msg["id"], "error": error})
            return
        if "method" in msg:  # notification
            if self._on_notification is not None:
                self._on_notification(str(msg["method"]), msg.get("params") or {})
            return
        fut = self._waiting.pop(msg.get("id"), None)  # type: ignore[arg-type]
        if fut is None or fut.done():
            return
        if "error" in msg:
            err = msg["error"] if isinstance(msg["error"], dict) else {}
            fut.set_exception(McpError(str(err.get("message", "error")), err.get("code")))
        else:
            fut.set_result(msg.get("result"))

    async def request(self, method: str, params: dict[str, Any] | None = None,
                      timeout: float = 60) -> Any:  # fmt: skip
        if self.closed.is_set():
            raise McpError("the MCP server isn't running")
        self._next_id += 1
        rid = self._next_id
        fut: asyncio.Future[Any] = asyncio.get_running_loop().create_future()
        self._waiting[rid] = fut
        msg: dict[str, Any] = {"jsonrpc": "2.0", "id": rid, "method": method}
        if params is not None:
            msg["params"] = params
        try:
            await self._send(msg)
            return await asyncio.wait_for(fut, timeout)
        except TimeoutError:
            with contextlib.suppress(Exception):
                await self.notify("notifications/cancelled",
                                  {"requestId": rid, "reason": "timed out"})  # fmt: skip
            raise McpError(f"{method}: no answer within {timeout:g}s") from None
        except (ConnectionError, OSError) as exc:
            raise McpError(f"the MCP server isn't reachable: {exc}") from None
        finally:
            self._waiting.pop(rid, None)

    async def notify(self, method: str, params: dict[str, Any] | None = None) -> None:
        msg: dict[str, Any] = {"jsonrpc": "2.0", "method": method}
        if params is not None:
            msg["params"] = params
        await self._send(msg)

    async def close(self) -> None:
        self._task.cancel()
        with contextlib.suppress(Exception):
            self._writer.close()
        with contextlib.suppress(asyncio.CancelledError, Exception):
            await self._task

    # ------------------------------------------------------------------ MCP

    async def initialize(self, timeout: float = 60) -> None:
        result = await self.request("initialize", {
            "protocolVersion": CLIENT_PROTOCOL,
            "capabilities": {},
            "clientInfo": {"name": "cairnward", "version": __version__},
        }, timeout=timeout)  # fmt: skip
        if not isinstance(result, dict):
            raise McpError("initialize: unexpected answer")
        self.server_info = dict(result.get("serverInfo") or {})
        self.protocol_version = str(result.get("protocolVersion", ""))
        await self.notify("notifications/initialized")

    async def list_tools(self, timeout: float = 60) -> list[dict[str, Any]]:
        tools: list[dict[str, Any]] = []
        cursor: str | None = None
        for _ in range(100):  # pages
            result = await self.request("tools/list", {"cursor": cursor} if cursor else {},
                                        timeout=timeout)  # fmt: skip
            if not isinstance(result, dict):
                raise McpError("tools/list: unexpected answer")
            tools += [t for t in result.get("tools", []) if isinstance(t, dict) and t.get("name")]
            cursor = result.get("nextCursor")
            if not cursor:
                break
        return tools

    async def call_tool(self, name: str, arguments: dict[str, Any], timeout: float) -> Any:
        return await self.request("tools/call", {"name": name, "arguments": arguments},
                                  timeout=timeout)  # fmt: skip
