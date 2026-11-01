"""The MCP proxy's core: aggregate the servers enabled for an environment, filter their
tools by pinning and policy, and check, audit and forward every call (spec §4.2).

Clients see each tool as `<server>__<tool>`. Server names can't contain "_", so the
first "__" splits them unambiguously.
"""

from __future__ import annotations

import asyncio
import contextlib
import time
from dataclasses import dataclass, field
from typing import Any

from cairnward.audit.log import AuditLog, canonical, digest
from cairnward.config.loader import Config
from cairnward.mcp.jsonrpc import Connection, McpError
from cairnward.mcp.launch import Launcher, LaunchError, Started
from cairnward.mcp.pins import PinStore, ToolState
from cairnward.policy.engine import Action, PolicyEngine

SEP = "__"


def exposed_name(server: str, tool: str) -> str:
    return f"{server}{SEP}{tool}"


@dataclass
class _Server:
    started: Started
    conn: Connection
    tools: list[dict[str, Any]] | None = None  # None: (re)list on next use


@dataclass
class ToolView:
    server: str
    definition: dict[str, Any]
    state: ToolState
    offered: bool
    why: str = ""


@dataclass
class McpHub:
    cfg: Config
    engine: PolicyEngine
    launcher: Launcher
    pins: PinStore
    audit: AuditLog | None = None
    _servers: dict[tuple[str, str], _Server] = field(default_factory=dict)
    _locks: dict[tuple[str, str], asyncio.Lock] = field(default_factory=dict)
    _reported: set[tuple[str, str, str]] = field(default_factory=set)

    # ------------------------------------------------------------------ connections

    def _audit(self, action: str, **kw: Any) -> None:
        if self.audit:
            self.audit.append(action, **kw)

    async def _server(self, server: str, environment: str) -> _Server:
        key = (server, environment)
        lock = self._locks.setdefault(key, asyncio.Lock())
        async with lock:
            current = self._servers.get(key)
            if current is not None and not current.conn.closed.is_set():
                return current
            if current is not None:  # it exited: clean up and start again
                await self._drop(key)
            spec = self.cfg.mcp.servers[server]
            try:
                started = await self.launcher.start(server, environment)
            except (LaunchError, OSError) as exc:
                self._audit("mcp.start", actor="cairnward", target=server, outcome="error",
                            environment=environment, details={"error": str(exc)[:300]})  # fmt: skip
                raise McpError(f"{server}: {exc}") from None

            def on_note(method: str, params: dict[str, Any]) -> None:
                if method == "notifications/tools/list_changed" and key in self._servers:
                    self._servers[key].tools = None

            conn = Connection(started.reader, started.writer, on_notification=on_note)
            try:
                await conn.initialize(timeout=min(spec.call_timeout_s, 120))
            except McpError as exc:
                await conn.close()
                await started.stop()
                self._audit("mcp.start", actor="cairnward", target=server, outcome="error",
                            environment=environment, details={"error": str(exc)[:300]})  # fmt: skip
                raise McpError(f"{server} didn't start: {exc} (see its log in the state "
                               "folder: logs/)") from None  # fmt: skip
            self._audit("mcp.start", actor="cairnward", target=server, outcome="ok",
                        environment=environment, details={
                            "sandboxed": started.sandboxed, "warnings": started.warnings,
                            "configured_version": spec.version,
                            "server_name": str(conn.server_info.get("name", ""))[:100],
                            "server_version": str(conn.server_info.get("version", ""))[:50],
                        })  # fmt: skip
            entry = _Server(started, conn)
            self._servers[key] = entry
            return entry

    async def _drop(self, key: tuple[str, str]) -> None:
        entry = self._servers.pop(key, None)
        if entry is not None:
            await entry.conn.close()
            await entry.started.stop()

    async def aclose(self) -> None:
        for key in list(self._servers):
            with contextlib.suppress(Exception):
                await self._drop(key)

    # ------------------------------------------------------------------ tools

    async def server_tools(self, server: str, environment: str) -> list[ToolView]:
        """Every tool of one server with its pin state and whether clients get it."""
        entry = await self._server(server, environment)
        if entry.tools is None:
            entry.tools = await entry.conn.list_tools(timeout=60)
        pinning = self.cfg.privacy(environment).value("mcp_pinning") == "on"
        views = []
        for tool in entry.tools:
            state = self.pins.state(server, tool)
            rule = self.engine.rule_for(Action("tool.call", f"{server}/{tool['name']}",
                                               environment=environment))  # fmt: skip
            offered, why = True, ""
            if rule.base == "deny":
                offered, why = False, rule.reason
            elif pinning and state.status != "approved":
                offered = False
                why = (f"{state.status} definition, not approved: cairnward mcp approve "
                       f"{server} --env {environment}")  # fmt: skip
            views.append(ToolView(server, tool, state, offered, why))
            mark = (server, tool["name"], state.hash)
            if state.status == "changed" and mark not in self._reported:
                self._reported.add(mark)
                self._audit(
                    "mcp.tool_changed",
                    actor="cairnward",
                    target=f"{server}/{tool['name']}",
                    environment=environment,
                    decision="deny" if pinning else "allow",
                    details={"hash": state.hash, "flags": state.flags, "disabled": pinning},
                )
        return views

    async def list_tools(self, environment: str) -> tuple[list[dict[str, Any]], list[str]]:
        """Tools offered to clients in this environment, and problems (servers that
        didn't start), so one broken server doesn't hide the others."""
        env = self.cfg.environments[environment]
        out: list[dict[str, Any]] = []
        problems: list[str] = []
        for server in env.mcp_servers:
            try:
                views = await self.server_tools(server, environment)
            except McpError as exc:
                problems.append(str(exc))
                continue
            for v in views:
                if v.offered:
                    out.append({**v.definition, "name": exposed_name(server, v.definition["name"])})
        return out, problems

    async def call(
        self,
        environment: str,
        name: str,
        arguments: dict[str, Any],
        *,
        client: str,
        run_id: str | None = None,
    ) -> dict[str, Any]:
        """A tools/call result (errors the model should see come back as isError)."""
        server, sep, tool = name.partition(SEP)
        env = self.cfg.environments[environment]
        if not sep or server not in env.mcp_servers:
            raise McpError(f"unknown tool '{name}'", -32602)
        views = await self.server_tools(server, environment)
        view = next((v for v in views if v.definition["name"] == tool), None)
        if view is None:
            raise McpError(f"unknown tool '{name}'", -32602)
        action = Action(
            kind="tool.call", target=f"{server}/{tool}", environment=environment,
            client=client, run_id=run_id,
            actor=f"agent:{client}" if run_id else f"client:{client}",
            details={"tool_hash": view.state.hash[:16]},
        )  # fmt: skip
        if not view.offered:
            from cairnward.policy.engine import Decision

            decision = Decision(action, "deny", "high", view.why)
        else:
            decision = await asyncio.to_thread(self.engine.check, action)
        if not decision.allowed:
            await asyncio.to_thread(self.engine.audit_deny, decision)
            return _tool_error(f"Cairnward refused this tool call: {decision.reason}")
        args_text = canonical(arguments)
        pre = await asyncio.to_thread(self.engine.audit_pre, decision, args_text)
        started = time.monotonic()
        spec = self.cfg.mcp.servers[server]
        try:
            entry = await self._server(server, environment)
            result = await entry.conn.call_tool(tool, arguments, timeout=spec.call_timeout_s)
            if not isinstance(result, dict):
                raise McpError("unexpected tools/call answer")
        except McpError as exc:
            await asyncio.to_thread(self.engine.audit_post, action, pre, outcome="error",
                                    details={"duration_s": round(time.monotonic() - started, 3),
                                             "error": str(exc)[:300]})  # fmt: skip
            return _tool_error(f"{name} failed: {exc}")
        text = canonical(result)
        await asyncio.to_thread(
            self.engine.audit_post, action, pre,
            outcome="error" if result.get("isError") else "ok", result_digest=digest(text),
            details={"duration_s": round(time.monotonic() - started, 3),
                     "result_bytes": len(text.encode())},
        )  # fmt: skip
        return result

    def approve(self, server: str, environment: str, approvals: dict[str, str],
                current: list[ToolView]) -> None:  # fmt: skip
        """Pin exactly the reviewed hashes; refuses if a definition changed meanwhile."""
        now = {v.definition["name"]: v.state.hash for v in current}
        stale = [n for n, h in approvals.items() if now.get(n) != h]
        if stale:
            raise ValueError(f"changed since you reviewed them: {', '.join(stale)}")
        entry = self._servers.get((server, environment))
        version = str(entry.conn.server_info.get("version", "")) if entry else ""
        self.pins.approve(server, approvals, version)
        self._audit("mcp.approve", actor="user", target=server, environment=environment,
                    decision="allow", details={"tools": sorted(approvals),
                                               "server_version": version[:50]})  # fmt: skip


def _tool_error(text: str) -> dict[str, Any]:
    return {"content": [{"type": "text", "text": text}], "isError": True}
