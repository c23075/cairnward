"""The MCP proxy's core: aggregate the servers enabled for an environment, filter their
tools by pinning and policy, and check, audit and forward every call (spec §4.2).

Clients see each tool as `<server>__<tool>`. Server names can't contain "_", so the
first "__" splits them unambiguously.
"""

from __future__ import annotations

import asyncio
import contextlib
import os
import re
import time
from dataclasses import dataclass, field, replace
from typing import TYPE_CHECKING, Any
from urllib.parse import urlsplit

from cairnward.audit.log import AuditLog, canonical, digest
from cairnward.config.loader import Config
from cairnward.mcp.jsonrpc import McpError
from cairnward.mcp.launch import Launcher, LaunchError, Started
from cairnward.mcp.pins import PinStore, ToolState
from cairnward.policy.engine import Action, PolicyEngine
from cairnward.policy.provenance import ProvenanceTracker
from cairnward.redaction import check_outbound, value_texts

if TYPE_CHECKING:
    # The MCP SDK takes about half a second to import, and the core only needs it once an MCP
    # server is used, so it's imported there (start time, I15).
    from cairnward.mcp.upstream import Connection

SEP = "__"
BUILTIN = "cairnward"  # prefix of Cairnward's own tools


def exposed_name(server: str, tool: str) -> str:
    return f"{server}{SEP}{tool}"


@dataclass
class _Server:
    started: Started | None  # None: a remote server (nothing to stop)
    conn: Connection
    tools: list[dict[str, Any]] | None = None  # None: (re)list on next use
    last_used: float = field(default_factory=time.monotonic)
    busy: int = 0  # calls in flight (never stopped while > 0)


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
    provenance: ProvenanceTracker | None = None
    vault: Any = None  # for Cairnward's own tools
    backends: Any = None  # the proxy for remote MCP servers
    plugins: Any = None  # plugins.services.PluginSessions
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
                current.last_used = time.monotonic()
                return current
            if current is not None:  # it exited: clean up and start again
                await self._drop(key)
            spec = self.cfg.mcp.servers[server]
            if spec.url is not None:
                remote = await self._remote(server, environment)
                self._servers[key] = remote
                return remote
            extra_env, plugin_run = self._plugin_env(server, environment)
            try:
                started = await self.launcher.start(server, environment, extra_env)
            except (LaunchError, OSError) as exc:
                if plugin_run:
                    self.plugins.revoke(plugin_run)
                self._audit("mcp.start", actor="cairnward", target=server, outcome="error",
                            environment=environment, details={"error": str(exc)[:300]})  # fmt: skip
                raise McpError(f"{server}: {exc}") from None

            def on_note(method: str, params: dict[str, Any]) -> None:
                if method == "notifications/tools/list_changed" and key in self._servers:
                    self._servers[key].tools = None

            if plugin_run:
                started = self._revoking(started, plugin_run)
            from cairnward.mcp.upstream import Connection

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

    def _plugin_env(self, server: str,
                    environment: str) -> tuple[dict[str, str] | None, str | None]:  # fmt: skip
        """A plugin's per-launch key and where its services are; (None, None) for other servers."""
        if self.cfg.mcp.servers[server].kind != "plugin":
            return None, None
        if self.plugins is None:
            raise McpError(f"{server}: plugins aren't available in this process")
        from cairnward.plugins import contract

        key, run_id = self.plugins.issue(self.cfg, server, environment)
        port = self.cfg.main.server.gateway_port
        return {contract.ENV_KEY: key, contract.ENV_GATEWAY: f"http://127.0.0.1:{port}/v1",
                contract.ENV_ENVIRONMENT: environment,
                contract.ENV_CONTRACT: str(contract.CONTRACT_VERSION)}, run_id  # fmt: skip

    def _revoking(self, started: Started, run_id: str) -> Started:
        stop = started.stop

        async def stop_and_revoke() -> None:
            self.plugins.revoke(run_id)
            await stop()

        return replace(started, stop=stop_and_revoke)

    async def _remote(self, server: str, environment: str) -> _Server:
        """A remote MCP server: its host is checked like any connection in the
        environment (off refuses, ask asks, an allow-list must list it), then the SDK's
        streamable-HTTP client connects through egress, so the egress report shows it."""
        spec = self.cfg.mcp.servers[server]
        url = spec.url or ""
        parts = urlsplit(url)
        port = parts.port or (443 if parts.scheme == "https" else 80)
        action = Action(kind="net.connect", target=f"{parts.hostname}:{port}",
                        environment=environment, actor="cairnward",
                        details={"via": "mcp", "server": server})  # fmt: skip
        decision = await asyncio.to_thread(self.engine.check, action)
        if not decision.allowed:
            await asyncio.to_thread(self.engine.audit_deny, decision)
            raise McpError(f"{server}: {parts.hostname} refused ({decision.reason})")

        def fail(why: str) -> McpError:
            self._audit("mcp.start", actor="cairnward", target=server, outcome="error",
                        environment=environment,
                        details={"error": why[:300], "remote": True})  # fmt: skip
            return McpError(f"{server}: {why}")

        try:
            headers = resolve_headers(spec.headers)
        except KeyError as exc:
            raise fail(
                f"environment variable {exc.args[0]} isn't set (used in its headers)"
            ) from None
        proxy = self.backends.web_proxy(environment) if self.backends is not None else None
        auth = None
        if spec.auth == "oauth":
            from cairnward.mcp import oauth

            auth = oauth.provider_for_core(self.cfg.paths, server, url, spec.oauth_scope)
        checked = {f"{parts.hostname}:{port}"}

        async def check_host(host: str, port_: int) -> None:
            """Other hosts (the sign-in server's discovery and token endpoints): checked
            like any connection in the environment, once each per connection."""
            target_ = f"{host}:{port_}"
            if target_ in checked:
                return
            act = Action(kind="net.connect", target=target_, environment=environment,
                         actor="cairnward", details={"via": "mcp", "server": server})  # fmt: skip
            dec = await asyncio.to_thread(self.engine.check, act)
            if not dec.allowed:
                await asyncio.to_thread(self.engine.audit_deny, dec)
                raise McpError(f"{server}: {host} refused ({dec.reason})")
            checked.add(target_)

        from cairnward.mcp.upstream import Connection, HttpTarget

        target = HttpTarget(url, headers, proxy, auth=auth, check_host=check_host)

        def on_note(method: str, params: dict[str, Any]) -> None:
            key = (server, environment)
            if method == "notifications/tools/list_changed" and key in self._servers:
                self._servers[key].tools = None

        conn = Connection(None, None, on_notification=on_note, http=target)
        try:
            await conn.initialize(timeout=min(spec.call_timeout_s, 60))
        except McpError as exc:
            await conn.close()
            problem = getattr(auth, "problem", None)
            if problem:
                raise fail(problem.removeprefix(f"{server} ")) from None
            raise fail(f"couldn't connect to {parts.hostname}: {exc}") from None
        self._audit("mcp.start", actor="cairnward", target=server, outcome="ok",
                    environment=environment, details={
                        "remote": True, "host": parts.hostname, "via_egress": proxy is not None,
                        "configured_version": spec.version,
                        "server_name": str(conn.server_info.get("name", ""))[:100],
                        "server_version": str(conn.server_info.get("version", ""))[:50],
                    })  # fmt: skip
        return _Server(None, conn)

    async def _drop(self, key: tuple[str, str]) -> None:
        entry = self._servers.pop(key, None)
        if entry is not None:
            await entry.conn.close()
            if entry.started is not None:
                await entry.started.stop()

    async def stop_idle(self, now: float | None = None) -> list[tuple[str, str]]:
        """Stop servers unused for longer than their idle_stop_minutes (0 = never); they
        start again on the next use. Returns (server, environment) of those stopped."""
        now = time.monotonic() if now is None else now
        stopped = []
        for key, entry in list(self._servers.items()):
            spec = self.cfg.mcp.servers.get(key[0])
            limit = (spec.idle_stop_minutes if spec else 0) * 60
            if not limit or entry.busy or now - entry.last_used < limit:
                continue
            async with self._locks.setdefault(key, asyncio.Lock()):
                if self._servers.get(key) is not entry or entry.busy:
                    continue
                await self._drop(key)
            idle = round(now - entry.last_used)
            self._audit("mcp.stop", actor="cairnward", target=key[0], environment=key[1],
                        details={"reason": "idle", "idle_s": idle})  # fmt: skip
            stopped.append(key)
        return stopped

    async def idle_loop(self, every_s: float = 60) -> None:
        while True:
            await asyncio.sleep(every_s)
            with contextlib.suppress(Exception):  # never take the server down for this
                await self.stop_idle()

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
        spec = self.cfg.mcp.servers.get(server)
        if spec is not None and spec.kind == "plugin":  # only its own audited switch turns it off
            pinning = not spec.unpinned_tools
        views = []
        for tool in entry.tools:
            state = self.pins.state(server, tool, entry.conn.raw_tools.get(tool["name"]))
            dropped = entry.conn.dropped_fields(tool)
            if dropped and (server, environment, f"dropped:{tool['name']}") not in self._reported:
                self._reported.add((server, environment, f"dropped:{tool['name']}"))
                self._audit("mcp.nonstandard", actor="cairnward",
                            target=f"{server}/{tool['name']}", environment=environment,
                            details={"dropped_fields": dropped[:20]})  # fmt: skip
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
        out: list[dict[str, Any]] = self._builtin_tools(environment)
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

    def _plugin_output(self, client: str, run_id: str | None, server: str) -> tuple[str, ...]:
        """Plugins whose output came before this call (rule 6). A plugin's own next tool
        gains nothing it didn't have, so it isn't counted."""
        if self.provenance is None:
            return ()
        return tuple(p for p in self.provenance.plugins(client, run_id) if p != server)

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
        if server == BUILTIN and any(t["name"] == name for t in self._builtin_tools(environment)):
            return await self._builtin(environment, tool, arguments, client=client, run_id=run_id)
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
            plugin_output=self._plugin_output(client, run_id, server),
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
        verdict = await asyncio.to_thread(
            check_outbound, self.engine, list(value_texts(arguments)),
            destination=f"mcp:{server}", environment=environment, client=client, run_id=run_id,
        )  # fmt: skip
        if not verdict.allowed:
            return _tool_error(f"Cairnward withheld this call: {verdict.reason}")
        args_text = canonical(arguments)
        pre = await asyncio.to_thread(self.engine.audit_pre, decision, args_text)
        started = time.monotonic()
        spec = self.cfg.mcp.servers[server]
        entry = None
        try:
            entry = await self._server(server, environment)
            entry.busy += 1
            try:
                result = await entry.conn.call_tool(tool, arguments, timeout=spec.call_timeout_s)
            finally:
                entry.busy -= 1
                entry.last_used = time.monotonic()
            if not isinstance(result, dict):
                raise McpError("unexpected tools/call answer")
        except McpError as exc:
            await asyncio.to_thread(self.engine.audit_post, action, pre, outcome="error",
                                    details={"duration_s": round(time.monotonic() - started, 3),
                                             "error": str(exc)[:300]})  # fmt: skip
            return _tool_error(f"{name} failed: {exc}")
        if self.provenance is not None:
            self.provenance.note_tool(client, run_id, f"{server}/{tool}")
            if spec.kind == "plugin":
                self.provenance.note_plugin(client, run_id, server)
        text = canonical(result)
        await asyncio.to_thread(
            self.engine.audit_post, action, pre,
            outcome="error" if result.get("isError") else "ok", result_digest=digest(text),
            details={"duration_s": round(time.monotonic() - started, 3),
                     "result_bytes": len(text.encode())},
        )  # fmt: skip
        return result

    # ------------------------------------------------------------------ Cairnward's own tools

    def _builtin_tools(self, environment: str) -> list[dict[str, Any]]:
        env = self.cfg.environments[environment]
        tools: list[dict[str, Any]] = []
        if "artifacts" in env.tools:
            tools += [
                {"name": exposed_name(BUILTIN, "artifact_write"),
                 "description": "Save a file (text) in this conversation's artifacts, versioned "
                                "by Cairnward: each write is a new version, nothing is lost.",
                 "inputSchema": {"type": "object", "required": ["path", "content"], "properties": {
                     "path": {"type": "string", "description": "relative path, e.g. notes/plan.md"},
                     "content": {"type": "string"}}}},
                {"name": exposed_name(BUILTIN, "artifact_read"),
                 "description": "Read a file from this conversation's artifacts.",
                 "inputSchema": {"type": "object", "required": ["path"],
                                 "properties": {"path": {"type": "string"}}},
                 "annotations": {"readOnlyHint": True}},
                {"name": exposed_name(BUILTIN, "artifact_list"),
                 "description": "List the files in this conversation's artifacts.",
                 "inputSchema": {"type": "object", "properties": {}},
                 "annotations": {"readOnlyHint": True}},
            ]  # fmt: skip
        if "vault" not in env.tools or self.vault is None:
            return tools
        return [*tools, {
            "name": exposed_name(BUILTIN, "lock_conversation"),
            "description": (
                "Lock the current conversation in Cairnward's transcript vault with a "
                "passphrase. The user confirms in Cairnward and types the passphrase there, "
                "never in this chat. Only use it when the user asks to lock or encrypt this "
                "conversation."
            ),
            "inputSchema": {"type": "object", "properties": {}},
        }]  # fmt: skip

    async def _builtin(self, environment: str, tool: str, arguments: dict[str, Any], *,
                       client: str, run_id: str | None) -> dict[str, Any]:  # fmt: skip
        """Cairnward's own tools; user-only ones (D46) are confirmed with provenance."""
        seen, conversation = (
            self.provenance.current(client, run_id) if self.provenance else ((), None)
        )
        if tool.startswith("artifact_"):
            return await self._artifact(environment, tool, arguments, client=client,
                                        run_id=run_id, conversation=conversation)  # fmt: skip
        if tool != "lock_conversation" or conversation is None:
            return _tool_error("Cairnward doesn't know which conversation this is yet: "
                               "lock it with `cairnward vault lock`")  # fmt: skip
        action = Action(
            kind="encrypt", target=conversation, environment=environment, client=client,
            run_id=run_id, conversation_id=conversation, initiated_by="model",
            actor=f"agent:{client}" if run_id else f"client:{client}", provenance=seen,
        )  # fmt: skip
        decision = await asyncio.to_thread(self.engine.check, action)
        if not decision.allowed or not decision.secret:
            await asyncio.to_thread(self.engine.audit_deny, decision)
            return _tool_error(f"Not locked: {decision.reason}")
        from cairnward.store.vault import VaultError

        try:
            await asyncio.to_thread(self.vault.lock, conversation, decision.secret)
        except VaultError as exc:
            return _tool_error(f"Not locked: {exc}")
        pre = await asyncio.to_thread(self.engine.audit_pre, decision)
        await asyncio.to_thread(self.engine.audit_post, action, pre, outcome="ok")
        return {"content": [{"type": "text", "text": (
            "Locked. The user set the passphrase in Cairnward; this conversation won't be "
            "sent to hosted models, and new messages are stored only while it's unlocked."
        )}]}  # fmt: skip

    async def _artifact(self, environment: str, tool: str, arguments: dict[str, Any], *,
                        client: str, run_id: str | None,
                        conversation: str | None) -> dict[str, Any]:  # fmt: skip
        from cairnward.artifacts import ArtifactError, ArtifactStore

        if conversation is None:
            return _tool_error("Cairnward doesn't know this conversation yet (send a message "
                               "through Cairnward first)")  # fmt: skip
        action = Action(kind="tool.call", target=f"{BUILTIN}/{tool}", environment=environment,
                        client=client, run_id=run_id, conversation_id=conversation,
                        actor=f"agent:{client}" if run_id else f"client:{client}",
                        plugin_output=self._plugin_output(client, run_id, BUILTIN),
                        details={"path": str(arguments.get("path", ""))[:200]})  # fmt: skip
        decision = await asyncio.to_thread(self.engine.check, action)
        if not decision.allowed:
            await asyncio.to_thread(self.engine.audit_deny, decision)
            return _tool_error(f"Cairnward refused this tool call: {decision.reason}")
        store = ArtifactStore(self.cfg.paths.data_dir, keys=self.vault)
        content = str(arguments.get("content", ""))
        pre = await asyncio.to_thread(self.engine.audit_pre, decision,
                                      content if tool == "artifact_write" else None)  # fmt: skip
        try:
            if tool == "artifact_write":
                rev = await asyncio.to_thread(store.write, "conversation", conversation,
                                              str(arguments.get("path", "")), content)  # fmt: skip
                text = f"saved {arguments.get('path')} (version {rev[:10]})"
            elif tool == "artifact_read":
                data = await asyncio.to_thread(store.read, "conversation", conversation,
                                               str(arguments.get("path", "")))  # fmt: skip
                text = data.decode("utf-8", "replace")
            else:
                files = await asyncio.to_thread(store.files, "conversation", conversation)
                text = "\n".join(files) or "(no artifacts yet)"
        except ArtifactError as exc:
            await asyncio.to_thread(self.engine.audit_post, action, pre, outcome="error",
                                    details={"error": str(exc)[:200]})  # fmt: skip
            return _tool_error(str(exc))
        await asyncio.to_thread(self.engine.audit_post, action, pre, outcome="ok")
        return {"content": [{"type": "text", "text": text}]}

    # ------------------------------------------------------------------ skills (prompts)

    def _skills(self, environment: str) -> dict[str, Any]:
        from cairnward import skills

        wanted = set(self.cfg.environments[environment].skills)
        found = skills.load(self.cfg.paths.config_dir / "skills")
        return {n: s for n, s in found.items() if n in wanted}

    def list_prompts(self, environment: str) -> list[dict[str, Any]]:
        return [{"name": n, "title": n.replace("-", " ").replace("_", " "),
                 "description": s.description, "arguments": []}
                for n, s in self._skills(environment).items()]  # fmt: skip

    def get_prompt(self, environment: str, name: str, client: str) -> dict[str, Any]:
        from cairnward import skills

        skill = self._skills(environment).get(name)
        if skill is None:
            raise McpError(f"unknown prompt '{name}'", -32602)
        self._audit("skill.use", actor=f"client:{client}", target=name, environment=environment)
        extra = skills.files(skill)
        text = skill.body
        if extra:
            text += "\n\nFiles of this skill (MCP resources): " + ", ".join(
                f"skill://{name}/{f}" for f in extra)  # fmt: skip
        return {
            "description": skill.description,
            "messages": [{"role": "user", "content": {"type": "text", "text": text}}],
        }

    def list_resources(self, environment: str) -> list[dict[str, Any]]:
        from cairnward import skills

        return [{"uri": f"skill://{n}/{f}", "name": f"{n}/{f}", "mimeType": "text/plain"}
                for n, s in self._skills(environment).items() for f in skills.files(s)]  # fmt: skip

    def read_resource(self, environment: str, uri: str) -> dict[str, Any]:
        from cairnward import skills

        rest = uri.removeprefix("skill://")
        name, _, rel = rest.partition("/")
        skill = self._skills(environment).get(name)
        if not uri.startswith("skill://") or skill is None:
            raise McpError(f"unknown resource '{uri}'", -32002)
        try:
            text = skills.read(skill, rel)
        except skills.SkillError as exc:
            raise McpError(str(exc), -32002) from None
        return {"contents": [{"uri": uri, "mimeType": "text/plain", "text": text}]}

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


_ENV_REF = re.compile(r"\$\{env:([A-Za-z_][A-Za-z0-9_]*)\}")


def resolve_headers(headers: dict[str, str]) -> dict[str, str]:
    """`${env:NAME}` in header values from Cairnward's environment (KeyError if unset)."""
    return {k: _ENV_REF.sub(lambda m: os.environ[m.group(1)], v) for k, v in headers.items()}


def _tool_error(text: str) -> dict[str, Any]:
    return {"content": [{"type": "text", "text": text}], "isError": True}
