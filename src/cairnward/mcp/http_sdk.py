"""The MCP SDK side of the gateway's /mcp endpoint (`mcp/http.py`): the server that answers
through the hub, and the SDK's session manager. Imported on the first /mcp request, not at start
(I15): the SDK takes about half a second to import."""

from __future__ import annotations

import asyncio
import contextlib
from collections.abc import Callable
from typing import Any

import mcp_types as types
from mcp.server.lowlevel import Server
from mcp.server.streamable_http_manager import StreamableHTTPSessionManager
from mcp.shared.exceptions import MCPError

from cairnward import __version__
from cairnward.mcp.http import _SCOPE_KEY, INSTRUCTIONS, _McpSession
from cairnward.mcp.hub import McpHub
from cairnward.mcp.jsonrpc import McpError


def _who(ctx: Any) -> _McpSession:
    request = getattr(ctx, "request", None)
    who = request.scope.get(_SCOPE_KEY) if request is not None else None
    if not isinstance(who, _McpSession):
        raise MCPError(types.INVALID_REQUEST, "no Cairnward identity on this request")
    return who


def _models(model: Any, items: list[dict[str, Any]]) -> list[Any]:
    """Standard objects only: an item that isn't valid MCP is left out, not an error."""
    out = []
    for item in items:
        with contextlib.suppress(ValueError, TypeError):
            out.append(model.model_validate(item))
    return out


def build_server(hub: McpHub) -> Server[Any]:
    async def list_tools(ctx: Any, params: Any) -> types.ListToolsResult:
        who = _who(ctx)
        tools, _problems = await hub.list_tools(who.environment)
        return types.ListToolsResult(tools=_models(types.Tool, tools))

    async def call_tool(ctx: Any, params: types.CallToolRequestParams) -> types.CallToolResult:
        who = _who(ctx)
        try:
            result = await hub.call(who.environment, params.name, dict(params.arguments or {}),
                                    client=who.client, run_id=who.run_id)  # fmt: skip
        except McpError as exc:
            raise MCPError(exc.code or types.INTERNAL_ERROR, str(exc)) from None
        except (ValueError, TypeError) as exc:  # arguments a built-in tool can't use
            raise MCPError(types.INVALID_PARAMS, f"invalid arguments: {str(exc)[:200]}") from None
        try:
            return types.CallToolResult.model_validate(result)
        except ValueError:
            return types.CallToolResult(is_error=True, content=[types.TextContent(
                type="text", text="the tool's answer wasn't valid MCP")])  # fmt: skip

    def guarded(fn: Callable[[], Any]) -> Any:
        try:
            return fn()
        except McpError as exc:
            raise MCPError(exc.code or types.INTERNAL_ERROR, str(exc)) from None

    async def list_prompts(ctx: Any, params: Any) -> types.ListPromptsResult:
        who = _who(ctx)
        return types.ListPromptsResult(prompts=_models(types.Prompt, guarded(
            lambda: hub.list_prompts(who.environment))))  # fmt: skip

    async def get_prompt(ctx: Any, params: types.GetPromptRequestParams) -> types.GetPromptResult:
        who = _who(ctx)
        raw = guarded(lambda: hub.get_prompt(who.environment, params.name, who.client))
        return types.GetPromptResult.model_validate(raw)

    async def list_resources(ctx: Any, params: Any) -> types.ListResourcesResult:
        who = _who(ctx)
        return types.ListResourcesResult(resources=_models(types.Resource, guarded(
            lambda: hub.list_resources(who.environment))))  # fmt: skip

    async def read_resource(
        ctx: Any, params: types.ReadResourceRequestParams
    ) -> types.ReadResourceResult:
        who = _who(ctx)
        raw = guarded(lambda: hub.read_resource(who.environment, str(params.uri)))
        return types.ReadResourceResult.model_validate(raw)

    async def list_templates(ctx: Any, params: Any) -> types.ListResourceTemplatesResult:
        return types.ListResourceTemplatesResult(resource_templates=[])

    return Server(
        "cairnward", version=__version__, title="Cairnward", instructions=INSTRUCTIONS,
        on_list_tools=list_tools, on_call_tool=call_tool, on_list_prompts=list_prompts,
        on_get_prompt=get_prompt, on_list_resources=list_resources,
        on_read_resource=read_resource, on_list_resource_templates=list_templates,
    )  # fmt: skip


class Manager:
    """The SDK's session manager, started on first use (so it also runs under test
    transports that don't send ASGI lifespan events) and stopped with the app."""

    def __init__(self, server: Server[Any]) -> None:
        # Stateless: every request carries the client's key and is checked on its own, so
        # server-side sessions would add no protection, only state to exhaust.
        self.manager = StreamableHTTPSessionManager(server, json_response=True, stateless=True)
        self._task: asyncio.Task[None] | None = None
        self._ready = asyncio.Event()
        self._stop = asyncio.Event()

    async def ensure(self) -> None:
        if self._task is None:
            self._task = asyncio.create_task(self._run())
        await self._ready.wait()

    async def _run(self) -> None:
        async with self.manager.run():
            self._ready.set()
            await self._stop.wait()

    async def aclose(self) -> None:
        self._stop.set()
        if self._task is not None:
            with contextlib.suppress(BaseException):
                await asyncio.wait_for(self._task, 5)
