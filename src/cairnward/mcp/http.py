"""The MCP endpoint on the gateway: streamable HTTP, JSON responses (spec §4.2, FR-MCP-1).

POST /mcp/<environment> (or /mcp: the client's default environment; for a
`cairnward run` key, the run's environment) with a client or run API key. Each POST
carries one JSON-RPC message; requests get a JSON reply, notifications 202. No
server-to-client stream is offered (GET answers 405, which the transport allows).
"""

from __future__ import annotations

import secrets
from collections.abc import Callable
from dataclasses import dataclass
from typing import Any
from urllib.parse import urlsplit

from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse, Response

from cairnward import __version__
from cairnward.config.models import LOOPBACK_HOSTS
from cairnward.mcp.hub import McpHub
from cairnward.mcp.jsonrpc import McpError

PROTOCOLS = ("2025-11-25", "2025-06-18", "2025-03-26", "2024-11-05")
INSTRUCTIONS = (
    "Tools from MCP servers enabled in this Cairnward environment, named "
    "<server>__<tool>. Every call is checked against your privacy settings and audited; "
    "some may wait for the user's approval."
)


@dataclass(frozen=True)
class _McpSession:
    client: str
    environment: str
    run_id: str | None


Identify = Callable[[Request], "tuple[str, str | None] | None"]


def _rpc_error(rid: Any, code: int, message: str, status: int = 200) -> JSONResponse:
    return JSONResponse({"jsonrpc": "2.0", "id": rid, "error": {"code": code, "message": message}},
                        status_code=status)  # fmt: skip


def add_mcp_routes(
    app: FastAPI,
    hub: McpHub,
    identify: Identify,
    *,
    default_environment: Callable[[str], str],
    run_environment: Callable[[str], str | None],
) -> None:
    sessions: dict[str, _McpSession] = {}
    cfg = hub.cfg

    async def handle(request: Request, path_env: str | None) -> Response:
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
        sid = request.headers.get("mcp-session-id")
        if sid is not None:
            known = sessions.get(sid)
            if known is None:
                return _rpc_error(None, -32600, "unknown session; initialize again", 404)
            if (known.client, known.environment, known.run_id) != (client, environment, run_id):
                return _rpc_error(None, -32600, "session belongs to another client", 403)
        try:
            msg = await request.json()
        except ValueError:
            return _rpc_error(None, -32700, "parse error", 400)
        if not isinstance(msg, dict) or msg.get("jsonrpc") != "2.0":
            return _rpc_error(None, -32600, "one JSON-RPC 2.0 message per request", 400)
        method, rid = msg.get("method"), msg.get("id")
        if method is None or "id" not in msg:  # notification, or a response to us
            return Response(status_code=202)
        params = msg.get("params") or {}
        headers: dict[str, str] = {}

        def ok(result: Any) -> JSONResponse:
            return JSONResponse({"jsonrpc": "2.0", "id": rid, "result": result}, headers=headers)

        if method == "initialize":
            asked = params.get("protocolVersion")
            new_sid = secrets.token_urlsafe(24)
            sessions[new_sid] = _McpSession(client, environment, run_id)
            headers["Mcp-Session-Id"] = new_sid
            return ok({
                "protocolVersion": asked if asked in PROTOCOLS else PROTOCOLS[1],
                "capabilities": {"tools": {"listChanged": False}, "prompts": {"listChanged": False},
                                 "resources": {"listChanged": False}},
                "serverInfo": {"name": "cairnward", "title": f"Cairnward ({environment})",
                               "version": __version__},
                "instructions": INSTRUCTIONS,
            })  # fmt: skip
        if method == "ping":
            return ok({})
        if method == "tools/list":
            tools, _problems = await hub.list_tools(environment)
            return ok({"tools": tools})
        if method == "tools/call":
            name, args = params.get("name"), params.get("arguments") or {}
            if not isinstance(name, str) or not isinstance(args, dict):
                return _rpc_error(rid, -32602, "tools/call needs a name and object arguments")
            try:
                return ok(await hub.call(environment, name, args, client=client, run_id=run_id))
            except McpError as exc:
                return _rpc_error(rid, exc.code or -32603, str(exc))
        try:
            if method == "prompts/list":
                return ok({"prompts": hub.list_prompts(environment)})
            if method == "prompts/get":
                return ok(hub.get_prompt(environment, str(params.get("name", "")), client))
            if method == "resources/list":
                return ok({"resources": hub.list_resources(environment)})
            if method == "resources/read":
                return ok(hub.read_resource(environment, str(params.get("uri", ""))))
        except McpError as exc:
            return _rpc_error(rid, exc.code or -32603, str(exc))
        if method == "resources/templates/list":
            return ok({"resourceTemplates": []})
        return _rpc_error(rid, -32601, f"method not found: {method}")

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
    @app.delete("/mcp/{environment}", include_in_schema=False)
    async def mcp_end(request: Request) -> Response:
        sid = request.headers.get("mcp-session-id")
        ident = identify(request)
        known = sessions.get(sid or "")
        if ident and known and known.client == ident[0]:
            sessions.pop(sid or "", None)
        return Response(status_code=204)
