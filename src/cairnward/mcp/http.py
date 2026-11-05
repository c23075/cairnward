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

from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from typing import Any
from urllib.parse import urlsplit

from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse, Response
from starlette.types import Receive, Scope, Send

from cairnward.config.models import LOOPBACK_HOSTS
from cairnward.mcp.hub import McpHub

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


class _LazyManager:
    """The SDK's server and session manager, built on the first /mcp request. Importing the MCP
    SDK takes about half a second, which every start would otherwise pay (I15)."""

    def __init__(self, hub: McpHub) -> None:
        self.hub = hub
        self._managed: Any = None

    @property
    def managed(self) -> Any:
        if self._managed is None:
            from cairnward.mcp.http_sdk import Manager, build_server

            self._managed = Manager(build_server(self.hub))
        return self._managed

    async def aclose(self) -> None:
        if self._managed is not None:
            await self._managed.aclose()


def add_mcp_routes(
    app: FastAPI,
    hub: McpHub,
    identify: Identify,
    *,
    default_environment: Callable[[str], str],
    run_environment: Callable[[str], str | None],
) -> None:
    cfg = hub.cfg
    lazy = _LazyManager(hub)
    app.state.mcp_manager = lazy

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
        managed = lazy.managed
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
