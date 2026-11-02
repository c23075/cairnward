"""MCP client for upstream servers, on the official MCP Python SDK (ADR-0015 amendment).

Cairnward starts each server itself (sandboxed; in isolated mode through a socket bridged
by the outer process), so the SDK's ClientSession runs over the byte streams Cairnward
already has: newline-delimited JSON-RPC, as in MCP's stdio transport. This module adapts
those asyncio streams to the SDK's and keeps the small interface the hub uses
(initialize, list_tools, call_tool, close, closed, server_info, protocol_version).

The SDK's typed layer keeps only the fields the MCP standard defines, so non-standard
fields never reach clients. Before parsing, each raw tools/list answer is kept
(`raw_tools`): to name the fields that were dropped (audited) and to carry over tool
approvals pinned before the switch, which hashed the raw definitions.

Servers can't use Cairnward's models or ask the user through it: sampling and
elicitation requests are refused (the SDK's defaults); roots/list answers with no roots.
"""

from __future__ import annotations

import asyncio
import contextlib
import json
from collections.abc import Callable
from typing import Any

import anyio
import mcp_types as types
from mcp.client.session import ClientSession
from mcp.shared.exceptions import MCPError
from mcp.shared.message import SessionMessage

from cairnward import __version__
from cairnward.mcp.jsonrpc import McpError

Notify = Callable[[str, dict[str, Any]], None]


def dump(model: Any) -> dict[str, Any]:
    """A standard MCP object as plain JSON (the fields it was given, camelCase)."""
    out: dict[str, Any] = model.model_dump(mode="json", by_alias=True, exclude_unset=True)
    return out


class Connection:
    """One upstream MCP server over a byte stream, via the SDK's ClientSession."""

    def __init__(self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter, *,
                 on_notification: Notify | None = None) -> None:  # fmt: skip
        self._reader, self._writer = reader, writer
        self._on_notification = on_notification
        self.closed = asyncio.Event()
        self.server_info: dict[str, Any] = {}
        self.protocol_version = ""
        self.raw_tools: dict[str, dict[str, Any]] = {}
        self._session: ClientSession | None = None
        self._ready = asyncio.Event()
        self._stop = anyio.Event()
        self._failure: BaseException | None = None
        self._task = asyncio.create_task(self._run())

    # ------------------------------------------------------------------ plumbing

    async def _run(self) -> None:
        to_session, from_server = anyio.create_memory_object_stream[SessionMessage | Exception](64)
        to_server, from_session = anyio.create_memory_object_stream[SessionMessage](64)
        eof = anyio.Event()

        async def pump_in() -> None:
            try:
                while True:
                    try:
                        line = await self._reader.readuntil(b"\n")
                    except (asyncio.IncompleteReadError, asyncio.LimitOverrunError,
                            ConnectionError):  # fmt: skip
                        break  # closed, or a message over the limit (can't resync)
                    if not line.strip():
                        continue
                    try:
                        raw = json.loads(line)
                        message = types.jsonrpc_message_adapter.validate_python(raw, by_name=False)
                    except ValueError:
                        continue  # servers sometimes print logs to stdout: not for us
                    self._tap(raw)
                    await to_session.send(SessionMessage(message))
            finally:
                eof.set()
                await to_session.aclose()

        async def pump_out() -> None:
            async with from_session:
                async for item in from_session:
                    data = item.message.model_dump_json(by_alias=True, exclude_unset=True)
                    self._writer.write(data.encode() + b"\n")
                    await self._writer.drain()

        async def handle(message: Any) -> None:
            if isinstance(message, Exception) or self._on_notification is None:
                return
            root = getattr(message, "root", message)
            method = str(getattr(root, "method", ""))
            params = getattr(root, "params", None)
            self._on_notification(method, dump(params) if params is not None else {})

        async def no_roots(context: Any) -> types.ListRootsResult:
            return types.ListRootsResult(roots=[])

        try:
            async with anyio.create_task_group() as tg:
                tg.start_soon(pump_in)
                tg.start_soon(pump_out)
                session = ClientSession(
                    from_server, to_server, message_handler=handle, list_roots_callback=no_roots,
                    client_info=types.Implementation(name="cairnward", version=__version__),
                )  # fmt: skip
                async with session:
                    self._session = session
                    self._ready.set()
                    with anyio.CancelScope() as scope:

                        async def stop_on_eof() -> None:
                            await eof.wait()
                            scope.cancel()

                        tg.start_soon(stop_on_eof)
                        await self._stop.wait()
                tg.cancel_scope.cancel()
        except BaseException as exc:
            self._failure = exc
            if isinstance(exc, asyncio.CancelledError):
                raise
        finally:
            self._session = None
            self.closed.set()
            self._ready.set()
            with contextlib.suppress(Exception):
                self._writer.close()

    def _tap(self, raw: Any) -> None:
        result = raw.get("result") if isinstance(raw, dict) else None
        tools = result.get("tools") if isinstance(result, dict) else None
        if isinstance(tools, list):
            for t in tools:
                if isinstance(t, dict) and isinstance(t.get("name"), str):
                    self.raw_tools[t["name"]] = t

    async def _call(
        self, what: str, timeout: float, coro_fn: Callable[[ClientSession], Any]
    ) -> Any:
        await self._ready.wait()
        session = self._session
        if session is None or self.closed.is_set():
            raise McpError("the MCP server isn't running")
        try:
            with anyio.fail_after(timeout):
                return await coro_fn(session)
        except TimeoutError:
            raise McpError(f"{what}: no answer within {timeout:g}s") from None
        except MCPError as exc:
            raise McpError(str(exc.error.message), exc.error.code) from None
        except (anyio.ClosedResourceError, anyio.BrokenResourceError, anyio.EndOfStream,
                ConnectionError, OSError):  # fmt: skip
            raise McpError("the MCP server closed the connection") from None
        except ValueError as exc:  # an answer that isn't valid MCP (pydantic)
            raise McpError(f"{what}: invalid answer from the server: {str(exc)[:300]}") from None

    async def close(self) -> None:
        self._stop.set()
        try:
            await asyncio.wait_for(asyncio.shield(self._task), 5)
        except (TimeoutError, asyncio.CancelledError, Exception):
            self._task.cancel()
            with contextlib.suppress(BaseException):
                await self._task

    # ------------------------------------------------------------------ MCP

    async def initialize(self, timeout: float = 60) -> None:
        result = await self._call("initialize", timeout, lambda s: s.initialize())
        self.server_info = dump(result.server_info) if result.server_info else {}
        self.protocol_version = str(result.protocol_version or "")

    async def list_tools(self, timeout: float = 60) -> list[dict[str, Any]]:
        tools: list[dict[str, Any]] = []
        cursor: str | None = None
        for _ in range(100):  # pages
            params = types.PaginatedRequestParams(cursor=cursor) if cursor else None

            def page(s: ClientSession, p: types.PaginatedRequestParams | None = params) -> Any:
                return s.list_tools(params=p)

            result = await self._call("tools/list", timeout, page)
            tools += [dump(t) for t in result.tools if t.name]
            cursor = result.next_cursor
            if not cursor:
                break
        return tools

    async def call_tool(self, name: str, arguments: dict[str, Any], timeout: float) -> Any:
        result = await self._call(f"tools/call {name}", timeout,
                                  lambda s: s.call_tool(name, arguments,
                                                        read_timeout_seconds=timeout))  # fmt: skip
        return dump(result)

    def dropped_fields(self, tool: dict[str, Any]) -> list[str]:
        """Non-standard top-level fields the server sent for this tool (not passed on)."""
        raw = self.raw_tools.get(str(tool.get("name")), {})
        return sorted(k for k in raw if k not in tool)
