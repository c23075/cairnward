"""Management API on a Unix socket (owner-only): used by `cairnward run` and the CLI.

Not reachable over TCP, so web pages and other users can't use it (spec §3.1, FR-API-2).
"""

from __future__ import annotations

import time
import uuid
from dataclasses import dataclass, field
from typing import Any, Literal

from fastapi import FastAPI, HTTPException
from pydantic import BaseModel, Field

from cairnward import __version__
from cairnward.audit.log import AuditLog
from cairnward.config.loader import Config
from cairnward.egress.addresses import literal_address, special_reason
from cairnward.egress.control import EgressControl
from cairnward.egress.proxy import Session
from cairnward.gateway.clients import RunKeys
from cairnward.gateway.scheduling import Budget
from cairnward.policy import (
    Action,
    ApprovalInbox,
    ApprovalRequest,
    Decision,
    PolicyEngine,
    host_matches,
)


class RunIn(BaseModel):
    environment: str
    client: str = Field(pattern=r"^[a-z0-9][a-z0-9._-]{0,62}$")
    profile: str
    workspace: str = Field(max_length=500)
    program: str = Field(max_length=200)
    arg_count: int = Field(ge=0)
    sandbox: str
    max_requests: int | None = Field(default=None, ge=1)
    max_tokens: int | None = Field(default=None, ge=1)
    max_minutes: float | None = Field(default=None, gt=0)


class RunEnd(BaseModel):
    exit_code: int
    duration_s: float
    changed_files: int | None = None


class AnswerIn(BaseModel):
    granted: bool
    scope: Literal["once", "conversation", "environment"] = "once"
    similar: bool = Field(default=False, description="also answer pending ones like it")
    secret: str | None = Field(default=None, max_length=1024, repr=False)


class SandboxAskIn(BaseModel):
    run_id: str = Field(max_length=64)
    host: str = Field(min_length=1, max_length=253)
    port: int = Field(ge=1, le=65535)


class AskIn(BaseModel):
    """An ApprovalRequest from the egress process (isolated mode)."""

    kind: str
    target: str = Field(max_length=500)
    risk: Literal["low", "medium", "high"]
    reason: str = Field(max_length=500)
    environment: str | None = None
    conversation_id: str | None = None
    client: str | None = None
    initiated_by: str = "model"
    provenance: list[str] = Field(default_factory=list, max_length=50)
    rememberable: bool = True
    run_id: str | None = None


class UnlockIn(BaseModel):
    passphrase: str = Field(min_length=1, max_length=1024)
    minutes: float = Field(default=30, gt=0, le=24 * 60)


class RelockIn(BaseModel):
    conversation_id: str | None = None


class McpApproveIn(BaseModel):
    environment: str | None = None
    tools: dict[str, str] = Field(description="tool name -> the definition hash you reviewed")


@dataclass
class _Run:
    environment: str
    client: str
    started: float
    approved: set[str] = field(default_factory=set)  # hosts allowed when the sandbox asked


def management_app(
    cfg: Config,
    *,
    audit: AuditLog | None,
    run_keys: RunKeys,
    egress: EgressControl | None,
    isolated: bool,
    engine: PolicyEngine | None = None,
    inbox: ApprovalInbox | None = None,
    mcp: Any = None,
    vault: Any = None,
    dashboard: Any = None,
) -> FastAPI:
    app = FastAPI(title="Cairnward management", docs_url=None, redoc_url=None)
    runs: dict[str, _Run] = {}
    server = cfg.main.server

    @app.get("/v1/status")
    def status() -> dict[str, Any]:
        return {
            "version": __version__,
            "isolated": isolated,
            "privacy": cfg.privacy().label,
            "gateway_port": server.gateway_port,
            "egress_port": server.egress_port if egress else None,
            "active_runs": len(runs),
        }

    @app.post("/v1/runs")
    def start_run(body: RunIn) -> dict[str, Any]:
        if body.environment not in cfg.environments:
            raise HTTPException(404, f"unknown environment '{body.environment}'")
        run_id = str(uuid.uuid4())
        env_budget = cfg.environments[body.environment].run_budget
        budget = Budget(
            max_requests=body.max_requests or env_budget.max_requests,
            max_tokens=body.max_tokens or env_budget.max_tokens,
            max_minutes=body.max_minutes or env_budget.max_minutes,
        )
        key = run_keys.issue(body.client, run_id, budget, body.environment)
        token = None
        if egress is not None:
            token = egress.create(
                Session(
                    environment=body.environment,
                    client=body.client,
                    run_id=run_id,
                    actor=f"agent:{body.client}",
                )
            )
        runs[run_id] = _Run(body.environment, body.client, time.monotonic())
        if audit:
            audit.append(
                "run.start",
                actor="user",
                environment=body.environment,
                client=body.client,
                run_id=run_id,
                target=body.program,
                details={
                    "profile": body.profile,
                    "workspace": body.workspace,
                    "arg_count": body.arg_count,
                    "sandbox": body.sandbox,
                    "budget": {k: v for k, v in budget.__dict__.items() if v},
                },
            )
        return {
            "run_id": run_id,
            "gateway_key": key,
            "egress_token": token,
            "gateway_port": server.gateway_port,
            "egress_port": server.egress_port if egress else None,
        }

    @app.post("/v1/runs/{run_id}/end")
    def end_run(run_id: str, body: RunEnd) -> dict[str, bool]:
        run = runs.pop(run_id, None)
        if run is None:
            raise HTTPException(404, "unknown run")
        run_keys.revoke_run(run_id)
        if egress is not None:
            egress.revoke_run(run_id)
        if audit:
            audit.append(
                "run.end",
                actor="user",
                environment=run.environment,
                client=run.client,
                run_id=run_id,
                outcome="ok" if body.exit_code == 0 else "error",
                details={
                    "exit_code": body.exit_code,
                    "duration_s": round(body.duration_s, 3),
                    "changed_files": body.changed_files,
                },
            )
        return {"ok": True}

    # ------------------------------------------------------------------ approvals (M2)

    @app.get("/v1/approvals")
    def approvals() -> dict[str, Any]:
        if inbox is None:
            return {"version": 0, "pending": []}
        return {"version": inbox.version, "pending": inbox.pending()}

    @app.get("/v1/approvals/wait")
    def approvals_wait(version: int = 0, timeout: float = 25) -> dict[str, Any]:
        """Long poll: returns when the inbox changes after `version` (or after `timeout`)."""
        if inbox is None:
            return {"version": 0, "pending": []}
        new = inbox.wait_for_change(version, min(max(timeout, 0), 60))
        return {"version": new, "pending": inbox.pending()}

    @app.post("/v1/approvals/ask")
    def ask(body: AskIn) -> dict[str, Any]:
        """Blocking: the egress process asks the user through this inbox."""
        if inbox is None:
            return {"granted": False, "scope": "once", "by": "no-approver"}
        req = ApprovalRequest(**{**body.model_dump(), "provenance": tuple(body.provenance)})
        got = inbox.request(req)
        return {"granted": got.granted, "scope": got.scope, "by": got.by}

    @app.post("/v1/approvals/sandbox")
    def sandbox_ask(body: SandboxAskIn) -> dict[str, Any]:
        """The sandbox met a host that isn't on its allow list (srt's ask callback).

        Decided by the policy engine for the run's environment (asking the user when
        network = ask). An allowed host stays allowed for the rest of the run, here and
        in egress, which the connection then goes through.
        """
        run = runs.get(body.run_id)
        if run is None or engine is None:
            return {"allow": False, "reason": "cairnward: unknown or finished run"}
        host = body.host.lower().strip("[]")
        target = f"[{host}]:{body.port}" if ":" in host else f"{host}:{body.port}"
        if target in run.approved:
            return {"allow": True}
        action = Action(
            kind="net.connect", target=target, environment=run.environment, client=run.client,
            run_id=body.run_id, actor=f"agent:{run.client}", details={"via": "sandbox"},
        )  # fmt: skip
        # The sandbox connects to this machine's addresses directly, not through egress,
        # so egress's guard is applied here: only addresses listed exactly (spec §3.2).
        ip = literal_address(host)
        why = special_reason(ip) if ip else None
        if ip and why:
            listed = engine.privacy_for(action).network_allow_hosts
            ip_target = f"[{ip}]:{body.port}" if ":" in ip else f"{ip}:{body.port}"
            if not any(host_matches(ip_target, p) or host_matches(ip, p) for p in listed):
                decision = Decision(action, "deny", "high", f"{host} is {why}; list the "
                                    "exact address in network_allow_hosts to allow it")  # fmt: skip
                engine.audit_deny(decision)
                return {"allow": False, "reason": f"cairnward: {target}: {decision.reason}"}
        decision = engine.check(action)
        if not decision.allowed:
            engine.audit_deny(decision)
            return {"allow": False, "reason": f"cairnward: {target}: {decision.reason}"}
        if body.run_id not in runs:  # the run ended while we were asking
            return {"allow": False, "reason": "cairnward: the run has ended"}
        run.approved.add(target)
        if egress is not None:
            egress.approve(body.run_id, target)
        if audit and decision.asked:
            audit.append(
                "approval", actor="user", target=target, decision="allow",
                environment=run.environment, client=run.client, run_id=body.run_id,
                details={"kind": "net.connect", "via": "sandbox", "scope": decision.scope,
                         "approved_by": decision.approved_by},
            )  # fmt: skip
        return {"allow": True}

    # ------------------------------------------------------------------ MCP servers

    def _mcp_env(server: str, environment: str | None) -> str:
        if server not in cfg.mcp.servers:
            raise HTTPException(404, f"unknown MCP server '{server}' (mcp.toml)")
        envs = [n for n, e in cfg.environments.items() if server in e.mcp_servers]
        if environment is None:
            if not envs:
                raise HTTPException(400, f"'{server}' isn't enabled in any environment "
                                    "(mcp_servers in environments/<name>.toml)")  # fmt: skip
            return envs[0]
        if environment not in envs:
            raise HTTPException(400, f"'{server}' isn't enabled in '{environment}'")
        return environment

    @app.get("/v1/mcp/servers")
    def mcp_servers() -> list[dict[str, Any]]:
        out = []
        for name, s in cfg.mcp.servers.items():
            out.append({
                "name": name, "description": s.description, "version": s.version,
                "command": s.command, "environments": [
                    n for n, e in cfg.environments.items() if name in e.mcp_servers],
                "permissions": s.permissions.model_dump(), "default_tool": s.default_tool,
                "tools": s.tools,
            })  # fmt: skip
        return out

    @app.get("/v1/mcp/{server}/tools")
    async def mcp_tools(server: str, environment: str | None = None) -> dict[str, Any]:
        from cairnward.mcp.jsonrpc import McpError

        env = _mcp_env(server, environment)
        if mcp is None:
            raise HTTPException(503, "the MCP proxy isn't running")
        try:
            views = await mcp.server_tools(server, env)
        except McpError as exc:
            raise HTTPException(502, str(exc)) from None
        return {"environment": env, "tools": [{
            "name": v.definition["name"], "description": v.definition.get("description", ""),
            "input_schema": v.definition.get("inputSchema"), "hash": v.state.hash,
            "status": v.state.status, "flags": v.state.flags, "offered": v.offered,
            "why": v.why,
        } for v in views]}  # fmt: skip

    @app.post("/v1/mcp/{server}/approve")
    async def mcp_approve(server: str, body: McpApproveIn) -> dict[str, Any]:
        """User-only (FR-POL-4): reachable only on the owner's management socket."""
        env = _mcp_env(server, body.environment)
        if mcp is None:
            raise HTTPException(503, "the MCP proxy isn't running")
        views = await mcp.server_tools(server, env)
        try:
            mcp.approve(server, env, body.tools, views)
        except ValueError as exc:
            raise HTTPException(409, str(exc)) from None
        return {"approved": sorted(body.tools)}

    @app.post("/v1/dashboard/link")
    def dashboard_link() -> dict[str, Any]:
        """A single-use sign-in link for the dashboard (only the owner can ask for one)."""
        if dashboard is None:
            raise HTTPException(503, "the dashboard isn't running")
        token = dashboard.new_link_token()
        return {"url": f"http://127.0.0.1:{server.dashboard_port}/login?t={token}"}

    # ------------------------------------------------------------------ vault (locks)

    @app.post("/v1/vault/{conversation_id}/unlock")
    def vault_unlock(conversation_id: str, body: UnlockIn) -> dict[str, Any]:
        """Open a locked conversation in the running core for a while (memory only).
        The passphrase arrives on the owner-only socket and is never stored or logged."""
        from cairnward.store.vault import VaultError

        if vault is None:
            raise HTTPException(503, "no vault in this process")
        try:
            vault.unlock(conversation_id, body.passphrase, minutes=body.minutes)
        except VaultError as exc:
            raise HTTPException(400, str(exc)) from None
        if audit:
            audit.append("vault.unlock", actor="user", target=conversation_id,
                         conversation_id=conversation_id,
                         details={"minutes": body.minutes, "where": "server"})  # fmt: skip
        return {"ok": True, "minutes": body.minutes}

    @app.post("/v1/vault/relock")
    def vault_relock(body: RelockIn) -> dict[str, bool]:
        if vault is not None:
            vault.relock(body.conversation_id)
        return {"ok": True}

    # declared last so that /ask and /sandbox above aren't taken for request ids
    @app.post("/v1/approvals/{request_id}")
    def answer(request_id: str, body: AnswerIn) -> dict[str, Any]:
        if inbox is None:
            raise HTTPException(404, "no such pending approval")
        if body.similar:
            count = inbox.answer_similar(request_id, body.granted, body.scope)
        else:
            count = int(inbox.answer(request_id, body.granted, body.scope, secret=body.secret))
        if not count:
            raise HTTPException(404, "no such pending approval (answered or expired)")
        return {"ok": True, "answered": count}

    return app
