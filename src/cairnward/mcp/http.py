"""The MCP endpoint on the gateway: streamable HTTP with JSON responses (spec §4.2,
FR-MCP-1), on the official MCP Python SDK (ADR-0015 amendment).

POST /mcp/<environment> (or /mcp: the client's default environment; for a
`cairnward run` key, the run's environment) with a client or run API key. Cairnward's
checks come first (Origin, API key, environment) on every request; then the SDK handles
the protocol (versions, initialize, message validation) in stateless mode: there are no
server-side sessions to hijack or exhaust, since each request is authorised by its own
key. No server-to-client stream is offered (GET answers 405, which the transport allows).

What clients can do is unchanged: tools (`<server>__<tool>` and Cairnward's own
`cairnward__*`), skills as prompts and resources; every call goes through the hub
(policy, approvals, pinning, audit).
"""

from __future__ import annotations

import asyncio
import contextlib
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from typing import Any
from urllib.parse import urlsplit

import mcp_types as types
from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse, Response
from mcp.server.lowlevel import Server
from mcp.server.streamable_http_manager import StreamableHTTPSessionManager
from mcp.shared.exceptions import MCPError
from starlette.types import Receive, Scope, Send

from cairnward import __version__
from cairnward.config.models import LOOPBACK_HOSTS
from cairnward.mcp.hub import McpHub
from cairnward.mcp.jsonrpc import McpError

INSTRUCTIONS = (
    "Tools from MCP servers enabled in this Cairnward environment, named "
    "<server>__<tool>. Every call is checked against your privacy settings and audited; "
    "some may wait for the user's approval."
)
_SCOPE_KEY = "cairnward.mcp"


@dataclass(frozen=True)
class _McpSession:
    client: str
    environment: str
    run_id: str | None


Identify = Callable[[Request], "tuple[str, str | None] | None"]


def _rpc_error(rid: Any, code: int, message: str, status: int = 200) -> JSONResponse:
    return JSONResponse({"jsonrpc": "2.0", "id": rid, "error": {"code": code, "message": message}},
                        status_code=status)  # fmt: skip


class _Asgi(Response):
    """Hands the request to an ASGI callable (the SDK's session manager)."""

    def __init__(self, call: Callable[[Scope, Receive, Send], Awaitable[None]]) -> None:
        super().__init__()
        self._call = call

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        await self._call(scope, receive, send)


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


class _Manager:
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


def add_mcp_routes(
    app: FastAPI,
    hub: McpHub,
    identify: Identify,
    *,
    default_environment: Callable[[str], str],
    run_environment: Callable[[str], str | None],
) -> None:
    cfg = hub.cfg
    managed = _Manager(build_server(hub))
    app.state.mcp_manager = managed

    def check(request: Request, path_env: str | None) -> _McpSession | JSONResponse:
        origin = request.headers.get("origin")
        if origin and (urlsplit(origin).hostname or "").lower() not in {
            h.strip("[]") for h in LOOPBACK_HOSTS
        }:
            return _rpc_error(None, -32600, "origin not allowed", 403)
        ident = identify(request)
        if ident is None:
            return _rpc_error(None, -32600, "missing or unknown API key (cairnward client add)",
                              401)  # fmt: skip
        client, run_id = ident
        if run_id:
            environment = run_environment(run_id) or default_environment(client)
            if path_env and path_env != environment:
                return _rpc_error(None, -32600, f"this run is in '{environment}'", 403)
        else:
            environment = path_env or default_environment(client)
        if environment not in cfg.environments:
            return _rpc_error(None, -32600, f"unknown environment '{environment}'", 404)
        return _McpSession(client, environment, run_id)

    async def handle(request: Request, path_env: str | None) -> Response:
        who = check(request, path_env)
        if isinstance(who, JSONResponse):
            return who
        await managed.ensure()

        async def delegate(scope: Scope, receive: Receive, send: Send) -> None:
            await managed.manager.handle_request({**scope, _SCOPE_KEY: who}, receive, send)

        return _Asgi(delegate)

    @app.post("/mcp", include_in_schema=False)
    async def mcp_default(request: Request) -> Response:
        return await handle(request, None)

    @app.post("/mcp/{environment}", include_in_schema=False)
    async def mcp_env(environment: str, request: Request) -> Response:
        return await handle(request, environment)

    @app.get("/mcp", include_in_schema=False)
    @app.get("/mcp/{environment}", include_in_schema=False)
    async def mcp_stream() -> Response:
        return Response(status_code=405, headers={"Allow": "POST, DELETE"})

    @app.delete("/mcp", include_in_schema=False)
    async def mcp_end_default(request: Request) -> Response:
        return await handle(request, None)

    @app.delete("/mcp/{environment}", include_in_schema=False)
    async def mcp_end(environment: str, request: Request) -> Response:
        return await handle(request, environment)
