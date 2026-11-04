"""How the core creates and revokes egress sessions.

Single-process mode: the core holds the egress `Sessions` object directly.
Isolated mode (T0.13): egress runs in another process; the core talks to its control
API on a Unix socket (owner-only permissions).
"""

from __future__ import annotations

import asyncio
from pathlib import Path
from typing import Any, Protocol

import httpx
from fastapi import FastAPI, HTTPException
from pydantic import BaseModel, Field

from cairnward.egress.proxy import Session, Sessions


class EgressControl(Protocol):
    def create(self, session: Session) -> str: ...
    def revoke_run(self, run_id: str) -> None: ...
    def approve(self, run_id: str, target: str) -> None: ...


class LocalEgress:
    def __init__(self, sessions: Sessions):
        self.sessions = sessions

    def create(self, session: Session) -> str:
        return self.sessions.create(session)

    def revoke_run(self, run_id: str) -> None:
        self.sessions.revoke_run(run_id)

    def approve(self, run_id: str, target: str) -> None:
        self.sessions.approve(run_id, target)


class SessionIn(BaseModel):
    environment: str
    client: str | None = None
    run_id: str | None = None
    actor: str = "cairnward"
    trusted_core: bool = False


class RevokeIn(BaseModel):
    run_id: str


class ApproveIn(BaseModel):
    run_id: str
    target: str = Field(max_length=300)


class McpStartIn(BaseModel):
    server: str = Field(max_length=31)
    environment: str = Field(max_length=63)
    extra_env: dict[str, str] = Field(default_factory=dict, repr=False)


class DoclingIn(BaseModel):
    environment: str = Field(max_length=63)
    files: list[str] = Field(min_length=1, max_length=5000)


class DeepRunIn(BaseModel):
    library: str = Field(max_length=200)
    question: str = Field(min_length=3, max_length=4000)
    key: str = Field(max_length=200, repr=False)
    gateway_port: int = Field(ge=1, le=65535)
    effort: str | None = Field(default=None, max_length=40)
    evidence_k: int = Field(default=10, ge=1, le=50)
    skip_summary: bool = False


def control_app(
    sessions: Sessions,
    mcp: Any = None,
    deep: Any = None,
    docling: Any = None,
    backup_now: Any = None,
) -> FastAPI:
    app = FastAPI(title="cairnward-egress control", docs_url=None, redoc_url=None)

    @app.get("/v1/health")
    def health() -> dict[str, bool]:
        return {"ok": True}

    @app.post("/v1/sessions")
    def create(body: SessionIn) -> dict[str, str]:
        return {"token": sessions.create(Session(**body.model_dump()))}

    @app.post("/v1/sessions/revoke")
    def revoke(body: RevokeIn) -> dict[str, bool]:
        sessions.revoke_run(body.run_id)
        return {"ok": True}

    @app.post("/v1/sessions/approve")
    def approve(body: ApproveIn) -> dict[str, bool]:
        sessions.approve(body.run_id, body.target)
        return {"ok": True}

    @app.post("/v1/mcp/start")
    async def mcp_start(body: McpStartIn) -> dict[str, Any]:
        """Start a configured MCP server (from this process's own config) for the core."""
        from cairnward.mcp.launch import LaunchError

        if mcp is None:
            raise HTTPException(404, "MCP servers aren't started here")
        try:
            result: dict[str, Any] = await mcp.start(body.server, body.environment, body.extra_env)
        except (LaunchError, OSError) as exc:
            raise HTTPException(400, str(exc)) from None
        return result

    @app.post("/v1/engines/paperqa/run")
    async def deep_run(body: DeepRunIn) -> dict[str, Any]:
        """Run the PaperQA2 worker for the isolated core (it can't start sandboxes). The
        papers, models and sandbox come from this process's own configuration."""
        from cairnward.research import LibraryError
        from cairnward.research.engines.paperqa import EngineError

        if deep is None:
            raise HTTPException(404, "the PaperQA2 engine isn't available here")
        try:
            result: dict[str, Any] = await deep(**body.model_dump())
        except (EngineError, LibraryError) as exc:
            raise HTTPException(400, str(exc)) from None
        return result

    @app.post("/v1/backup/run")
    async def backup_run() -> dict[str, Any]:
        """A backup asked for through the isolated core, which can't reach the folder."""
        from cairnward.backup import BackupError

        if backup_now is None:
            raise HTTPException(404, "backups aren't available here")
        try:
            result: dict[str, Any] = await asyncio.to_thread(backup_now)
        except BackupError as exc:
            raise HTTPException(400, str(exc)) from None
        return result

    @app.post("/v1/engines/docling/parse")
    async def docling_parse(body: DoclingIn) -> dict[str, Any]:
        """Run the Docling worker for the isolated core (it can't start sandboxes)."""
        from cairnward.research.engines.docling import DoclingError

        if docling is None:
            raise HTTPException(404, "Docling isn't available here")
        try:
            results = await docling(body.environment, [Path(f) for f in body.files])
        except DoclingError as exc:
            raise HTTPException(400, str(exc)) from None
        return {"results": results}

    return app


class RemoteEgress:
    def __init__(self, socket_path: str):
        self._client = httpx.Client(
            transport=httpx.HTTPTransport(uds=socket_path), base_url="http://egress", timeout=10
        )

    def _post(self, path: str, body: dict[str, Any]) -> dict[str, Any]:
        resp = self._client.post(path, json=body)
        resp.raise_for_status()
        data: dict[str, Any] = resp.json()
        return data

    def create(self, session: Session) -> str:
        return str(self._post("/v1/sessions", session.__dict__)["token"])

    def revoke_run(self, run_id: str) -> None:
        self._post("/v1/sessions/revoke", {"run_id": run_id})

    def approve(self, run_id: str, target: str) -> None:
        self._post("/v1/sessions/approve", {"run_id": run_id, "target": target})

    def alive(self) -> bool:
        try:
            return self._client.get("/v1/health", timeout=2).status_code == 200
        except httpx.HTTPError:
            return False
