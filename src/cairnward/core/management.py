"""Management API on a Unix socket (owner-only): used by `cairnward run` and the CLI.

Not reachable over TCP, so web pages and other users can't use it (spec §3.1, FR-API-2).
"""

from __future__ import annotations

import time
import uuid
from dataclasses import dataclass
from typing import Any

from fastapi import FastAPI, HTTPException
from pydantic import BaseModel, Field

from cairnward import __version__
from cairnward.audit.log import AuditLog
from cairnward.config.loader import Config
from cairnward.egress.control import EgressControl
from cairnward.egress.proxy import Session
from cairnward.gateway.clients import RunKeys
from cairnward.gateway.scheduling import Budget


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


@dataclass
class _Run:
    environment: str
    client: str
    started: float


def management_app(
    cfg: Config,
    *,
    audit: AuditLog | None,
    run_keys: RunKeys,
    egress: EgressControl | None,
    isolated: bool,
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

    return app
