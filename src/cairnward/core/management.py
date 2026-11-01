"""Management API on a Unix socket (owner-only): used by `cairnward run` and the CLI.

Not reachable over TCP, so web pages and other users can't use it (spec §3.1, FR-API-2).
"""

from __future__ import annotations

import socket
import time
import uuid
from dataclasses import dataclass, field
from typing import Any, Literal

from fastapi import FastAPI, HTTPException
from pydantic import BaseModel, Field

from cairnward import __version__
from cairnward.audit.log import AuditLog
from cairnward.config.loader import Config
from cairnward.egress.addresses import is_ip_literal, special_reason
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


def _literal_ip(host: str) -> str | None:
    """The address a host string denotes without DNS: IP literals, inet_aton shorthand
    such as 127.1 or 2130706433, and localhost names."""
    if is_ip_literal(host):
        return host.strip("[]")
    if host == "localhost" or host.endswith(".localhost"):
        return "127.0.0.1"
    if host.replace(".", "").replace("x", "").isalnum() and host[:1].isdigit():
        try:
            return socket.inet_ntoa(socket.inet_aton(host))
        except OSError:
            return None
    return None


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
        key = run_keys.issue(body.client, run_id, budget)
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
        ip = _literal_ip(host)
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

    # declared last so that /ask and /sandbox above aren't taken for request ids
    @app.post("/v1/approvals/{request_id}")
    def answer(request_id: str, body: AnswerIn) -> dict[str, bool]:
        if inbox is None or not inbox.answer(request_id, body.granted, body.scope):
            raise HTTPException(404, "no such pending approval (answered or timed out)")
        return {"ok": True}

    return app
