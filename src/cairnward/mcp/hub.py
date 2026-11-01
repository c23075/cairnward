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
from pathlib import Path
from typing import Any

from cairnward.audit.log import AuditLog, canonical, digest
from cairnward.config.loader import Config
from cairnward.mcp.jsonrpc import Connection, McpError
from cairnward.mcp.launch import Launcher, LaunchError, Started
from cairnward.mcp.pins import PinStore, ToolState
from cairnward.policy.engine import Action, PolicyEngine
from cairnward.policy.provenance import ProvenanceTracker
from cairnward.redaction import check_outbound, value_texts

SEP = "__"
BUILTIN = "cairnward"  # prefix of Cairnward's own tools


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
    provenance: ProvenanceTracker | None = None
    vault: Any = None  # for Cairnward's own tools
    backends: Any = None  # for research tools (embeddings through the gateway's drivers)
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
        if self.provenance is not None:
            self.provenance.note_tool(client, run_id, f"{server}/{tool}")
        text = canonical(result)
        await asyncio.to_thread(
            self.engine.audit_post, action, pre,
            outcome="error" if result.get("isError") else "ok", result_digest=digest(text),
            details={"duration_s": round(time.monotonic() - started, 3),
                     "result_bytes": len(text.encode())},
        )  # fmt: skip
        return result

    # ------------------------------------------------------------------ Cairnward's own tools

    def _libraries(self, environment: str) -> list[str]:
        from cairnward.research import Research

        with contextlib.closing(Research.open(self.cfg.paths)) as research:
            return [lib.name for lib, _docs, _chunks in research.list_libraries()
                    if lib.environment == environment and lib.embedding_dim]  # fmt: skip

    def _builtin_tools(self, environment: str) -> list[dict[str, Any]]:
        env = self.cfg.environments[environment]
        tools: list[dict[str, Any]] = []
        if "research" in env.tools and self.backends is not None:
            libraries = self._libraries(environment)
            if libraries:
                tools.append({
                    "name": exposed_name(BUILTIN, "lit_search"),
                    "description": (
                        "Search the user's literature library for passages relevant to a "
                        "question (vector + keyword search). Returns passages with document "
                        "titles and page numbers; cite them as [title, p. N]."
                    ),
                    "inputSchema": {"type": "object", "required": ["query"], "properties": {
                        "query": {"type": "string", "description": "what to look for"},
                        "library": {"type": "string", "enum": libraries},
                        "k": {"type": "integer", "minimum": 1, "maximum": 20, "default": 8},
                    }},
                    "annotations": {"readOnlyHint": True},
                })  # fmt: skip
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
        """User-only actions (D46): always confirmed by the user, with provenance."""
        seen, conversation = (
            self.provenance.current(client, run_id) if self.provenance else ((), None)
        )
        if tool == "lit_search":
            return await self._lit_search(environment, arguments, client=client, run_id=run_id)
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

    async def _lit_search(self, environment: str, arguments: dict[str, Any], *, client: str,
                          run_id: str | None) -> dict[str, Any]:  # fmt: skip
        from cairnward.research import InProcessEmbedder, Research

        libraries = self._libraries(environment)
        name = arguments.get("library") or (libraries[0] if len(libraries) == 1 else None)
        query = str(arguments.get("query", "")).strip()
        if name not in libraries or not query:
            return _tool_error(f"give a query and one of the libraries: {', '.join(libraries)}")
        k = max(1, min(int(arguments.get("k", 8) or 8), 20))
        action = Action(
            kind="tool.call", target=f"{BUILTIN}/lit_search", environment=environment,
            client=client, run_id=run_id,
            actor=f"agent:{client}" if run_id else f"client:{client}",
            details={"library": name, "k": k},
        )  # fmt: skip
        decision = await asyncio.to_thread(self.engine.check, action)
        if not decision.allowed:
            await asyncio.to_thread(self.engine.audit_deny, decision)
            return _tool_error(f"Cairnward refused this tool call: {decision.reason}")
        pre = await asyncio.to_thread(self.engine.audit_pre, decision, query)
        embedder = InProcessEmbedder(self.cfg, self.engine, self.backends, environment=environment)
        research = Research.open(self.cfg.paths)
        try:
            hits = await research.search(name, query, embedder, k=k)
        except Exception as exc:  # library and embedding backend errors alike
            await asyncio.to_thread(self.engine.audit_post, action, pre, outcome="error",
                                    details={"error": str(exc)[:300]})  # fmt: skip
            return _tool_error(f"search failed: {exc}")
        finally:
            research.close()
        if self.provenance is not None:  # document text is untrusted content
            self.provenance.note_tool(client, run_id, f"lit_search in {name}")
        await asyncio.to_thread(self.engine.audit_post, action, pre, outcome="ok",
                                details={"hits": len(hits)})  # fmt: skip
        lines = [f"[{h.title or Path(h.path).name}, p. {h.pages}] {h.text}" for h in hits]
        return {
            "content": [{"type": "text", "text": "\n\n".join(lines) or "No passages found."}],
            "structuredContent": {"hits": [{
                "title": h.title, "file": Path(h.path).name, "pages": h.pages, "text": h.text,
                "score": round(h.score, 4)} for h in hits]},
        }  # fmt: skip

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


def _tool_error(text: str) -> dict[str, Any]:
    return {"content": [{"type": "text", "text": text}], "isError": True}
