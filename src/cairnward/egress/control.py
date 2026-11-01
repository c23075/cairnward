"""How the core creates and revokes egress sessions.

Single-process mode: the core holds the egress `Sessions` object directly.
Isolated mode (T0.13): egress runs in another process; the core talks to its control
API on a Unix socket (owner-only permissions).
"""

from __future__ import annotations

from typing import Any, Protocol

import httpx
from fastapi import FastAPI
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


def control_app(sessions: Sessions) -> FastAPI:
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
