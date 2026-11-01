"""The dashboard: Cairnward's only UI, a loopback web app (spec §3.1, §4.9, FR-DASH-1..3).

Security (ADR-0005):
- served on 127.0.0.1 only; requests must name it in the Host header (DNS rebinding);
- no passwords: `cairnward dashboard` asks the running core (owner-only socket) for a
  single-use login link that expires in 2 minutes; it becomes an HttpOnly,
  SameSite=Strict session cookie;
- state-changing requests need the session's CSRF token in a header and, when the
  browser sends one, an Origin of this dashboard; no CORS headers are ever sent;
- a strict Content-Security-Policy (scripts and styles from this origin only, no
  framing); the page builds every element with textContent, never HTML from data.

Static files are plain ES modules (no build step, ADR-0016).
"""

from __future__ import annotations

import asyncio
import dataclasses
import secrets
import time
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Literal
from urllib.parse import urlsplit

from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import (
    FileResponse,
    JSONResponse,
    PlainTextResponse,
    RedirectResponse,
    Response,
)
from pydantic import BaseModel, Field

from cairnward import __version__
from cairnward.audit.log import AuditLog
from cairnward.config.loader import Config
from cairnward.config.models import LOOPBACK_HOSTS

STATIC = Path(__file__).with_name("static")
SESSION_COOKIE = "cw_session"
LINK_TTL_S = 120
SESSION_IDLE_S = 12 * 3600
CSP = (
    "default-src 'none'; script-src 'self'; style-src 'self'; img-src 'self' data:; "
    "connect-src 'self'; font-src 'self'; frame-ancestors 'none'; base-uri 'none'; "
    "form-action 'self'"
)


@dataclass
class DashboardAuth:
    """Single-use login links and sessions (in memory: a restart signs everyone out)."""

    _links: dict[str, float] = field(default_factory=dict)
    _sessions: dict[str, tuple[str, float]] = field(default_factory=dict)  # id -> (csrf, seen)

    def new_link_token(self) -> str:
        now = time.monotonic()
        self._links = {t: exp for t, exp in self._links.items() if exp > now}
        token = secrets.token_urlsafe(32)
        self._links[token] = now + LINK_TTL_S
        return token

    def redeem(self, token: str) -> str | None:
        exp = self._links.pop(token, None)
        if exp is None or exp < time.monotonic():
            return None
        sid = secrets.token_urlsafe(32)
        self._sessions[sid] = (secrets.token_urlsafe(24), time.monotonic())
        return sid

    def session(self, sid: str | None) -> str | None:
        """The session's CSRF token, or None when it's unknown or idle too long."""
        if not sid or sid not in self._sessions:
            return None
        csrf, seen = self._sessions[sid]
        if time.monotonic() - seen > SESSION_IDLE_S:
            del self._sessions[sid]
            return None
        self._sessions[sid] = (csrf, time.monotonic())
        return csrf

    def end(self, sid: str | None) -> None:
        self._sessions.pop(sid or "", None)


# ------------------------------------------------------------------ request bodies


class AnswerIn(BaseModel):
    granted: bool
    scope: Literal["once", "conversation", "environment"] = "once"
    similar: bool = False
    secret: str | None = Field(default=None, max_length=1024, repr=False)


class PassIn(BaseModel):
    passphrase: str = Field(min_length=1, max_length=1024, repr=False)
    minutes: float = Field(default=30, gt=0, le=24 * 60)


class TitleIn(BaseModel):
    title: str = Field(min_length=1, max_length=300)


class FolderIn(BaseModel):
    name: str = Field(min_length=1, max_length=100)


class MoveIn(BaseModel):
    folder_id: str | None = None


class ApproveToolsIn(BaseModel):
    environment: str | None = None
    tools: dict[str, str]


def _record(r: Any) -> dict[str, Any]:
    return dataclasses.asdict(r)


def dashboard_app(
    cfg: Config,
    *,
    auth: DashboardAuth,
    audit: AuditLog | None,
    vault: Any = None,
    inbox: Any = None,
    mcp: Any = None,
    isolated: bool = False,
    jobs: Any = None,
) -> FastAPI:
    app = FastAPI(title="Cairnward dashboard", docs_url=None, redoc_url=None, openapi_url=None)
    port = cfg.main.server.dashboard_port
    origins = {f"http://{h}:{port}" for h in LOOPBACK_HOSTS}

    # ------------------------------------------------------------------ security layer

    @app.middleware("http")
    async def guard(
        request: Request, call_next: Callable[[Request], Awaitable[Response]]
    ) -> Response:
        host = request.headers.get("host", "")
        name = host[: host.find("]") + 1] if host.startswith("[") else host.split(":", 1)[0]
        if name.lower() not in LOOPBACK_HOSTS:
            return PlainTextResponse(f"use http://127.0.0.1:{port}/", status_code=421)
        path = request.url.path
        if path.startswith("/api/"):
            csrf = auth.session(request.cookies.get(SESSION_COOKIE))
            if csrf is None:
                return JSONResponse({"detail": "not signed in: run `cairnward dashboard`"},
                                    status_code=401)  # fmt: skip
            if request.method not in ("GET", "HEAD"):
                origin = request.headers.get("origin")
                if origin is not None and origin not in origins:
                    return JSONResponse({"detail": "cross-origin request refused"}, status_code=403)
                if not secrets.compare_digest(request.headers.get("x-csrf-token", ""), csrf):
                    return JSONResponse({"detail": "missing or wrong CSRF token"}, status_code=403)
        response = await call_next(request)
        response.headers["Content-Security-Policy"] = CSP
        response.headers["X-Content-Type-Options"] = "nosniff"
        response.headers["Referrer-Policy"] = "no-referrer"
        response.headers["X-Frame-Options"] = "DENY"
        response.headers["Cross-Origin-Opener-Policy"] = "same-origin"
        response.headers["Cross-Origin-Resource-Policy"] = "same-origin"
        if path.startswith("/api/") or path == "/login":
            response.headers["Cache-Control"] = "no-store"
        return response

    @app.get("/login", include_in_schema=False)
    def login(t: str = "") -> Response:
        sid = auth.redeem(t)
        if sid is None:
            return PlainTextResponse(
                "This login link was already used or has expired. Run `cairnward dashboard` "
                "for a new one.", status_code=403)  # fmt: skip
        resp = RedirectResponse("/", status_code=303)  # drops the token from the address bar
        resp.set_cookie(SESSION_COOKIE, sid, httponly=True, samesite="strict", path="/",
                        max_age=SESSION_IDLE_S)  # fmt: skip
        if audit:
            audit.append("dashboard.login", actor="user")
        return resp

    @app.post("/api/logout")
    def logout(request: Request) -> Response:
        auth.end(request.cookies.get(SESSION_COOKIE))
        resp = JSONResponse({"ok": True})
        resp.delete_cookie(SESSION_COOKIE, path="/")
        return resp

    # ------------------------------------------------------------------ static files

    files = {p.name: p for p in STATIC.rglob("*") if p.is_file()}
    types = {".js": "text/javascript", ".css": "text/css", ".html": "text/html",
             ".svg": "image/svg+xml"}  # fmt: skip

    @app.get("/", include_in_schema=False)
    def index() -> Response:
        return FileResponse(STATIC / "index.html", media_type="text/html")

    @app.get("/static/{name}", include_in_schema=False)
    def static(name: str) -> Response:
        path = files.get(name)
        if path is None:
            raise HTTPException(404)
        return FileResponse(path, media_type=types.get(path.suffix, "application/octet-stream"))

    # ------------------------------------------------------------------ overview

    @app.get("/api/session")
    def session(request: Request) -> dict[str, Any]:
        eff = cfg.privacy()
        return {
            "csrf": auth.session(request.cookies.get(SESSION_COOKIE)),
            "version": __version__, "isolated": isolated, "privacy": eff.label,
            "weakened": eff.weaker_than_strict, "warnings": eff.warnings,
            "pending": len(inbox.pending()) if inbox else 0,
        }  # fmt: skip

    @app.get("/api/settings")
    def settings() -> dict[str, Any]:
        b = cfg.backends
        return {
            "global": cfg.privacy().as_dict(),
            "environments": {
                name: {"privacy": cfg.privacy(name).as_dict(), "description": e.description,
                       "workspace": [str(w) for w in e.workspace], "mcp_servers": e.mcp_servers,
                       "tools": e.tools, "retention_days": e.vault_retention_days}
                for name, e in cfg.environments.items()
            },
            "backends": {n: {"driver": x.driver, "location": x.location,
                             "host": urlsplit(x.url).netloc} for n, x in b.backends.items()},
            "roles": {r: f"{m.model} on {m.backend}" for r, m in b.roles.items()},
            "clients": {n: {"kind": c.kind, "profile": c.profile,
                            "default_environment": c.default_environment}
                        for n, c in cfg.clients.clients.items()},
            "anchor_repo": cfg.main.anchor.repo,
        }  # fmt: skip

    # ------------------------------------------------------------------ approvals

    def need_inbox() -> Any:
        if inbox is None:
            raise HTTPException(503, "no approvals inbox in this process")
        return inbox

    @app.get("/api/approvals")
    def approvals() -> dict[str, Any]:
        box = need_inbox()
        return {"version": box.version, "pending": box.pending()}

    @app.get("/api/approvals/wait")
    async def approvals_wait(version: int = 0, timeout: float = 25) -> dict[str, Any]:
        box = need_inbox()
        new = await asyncio.to_thread(box.wait_for_change, version, min(max(timeout, 0), 30))
        return {"version": new, "pending": box.pending()}

    @app.post("/api/approvals/{request_id}")
    def answer(request_id: str, body: AnswerIn) -> dict[str, Any]:
        box = need_inbox()
        if body.similar:
            count = box.answer_similar(request_id, body.granted, body.scope, by="user (dashboard)")
        else:
            count = int(box.answer(request_id, body.granted, body.scope, by="user (dashboard)",
                                   secret=body.secret))  # fmt: skip
        if not count:
            raise HTTPException(404, "already answered or expired")
        return {"answered": count}

    # ------------------------------------------------------------------ audit and egress

    def need_audit() -> AuditLog:
        if audit is None:
            raise HTTPException(503, "the audit log isn't open in this process")
        return audit

    @app.get("/api/activity")
    def activity(after: int = 0, limit: int = 100) -> list[dict[str, Any]]:
        recs = need_audit().records(last=min(max(limit, 1), 500))
        return [_record(r) for r in recs if r.seq > after]

    @app.get("/api/audit")
    def audit_list(action: str | None = None, client: str | None = None, run: str | None = None,
                   env: str | None = None, conversation: str | None = None,
                   since: str | None = None, limit: int = 200) -> list[dict[str, Any]]:  # fmt: skip
        from cairnward.audit.egress_report import parse_since, record_time

        try:
            cutoff = parse_since(since) if since else None
        except ValueError:
            raise HTTPException(400, "since: use e.g. 24h, 7d or an ISO date") from None
        recs = need_audit().records(action=action or None, conversation_id=None)
        out = [
            r for r in recs
            if (not client or r.client == client) and (not run or (r.run_id or "").startswith(run))
            and (not env or r.environment == env) and (not cutoff or record_time(r) >= cutoff)
            and (not conversation or (r.conversation_id or r.details.get("conversation") or "")
                 .startswith(conversation))
        ]  # fmt: skip
        return [_record(r) for r in out[-min(max(limit, 1), 2000) :]]

    @app.get("/api/audit/verify")
    def audit_verify() -> dict[str, Any]:
        res = need_audit().verify()
        return {"ok": res.ok, "records": res.records, "problems": res.problems,
                "notes": res.notes, "head": f"{res.head_seq}:{res.head_hash}"}  # fmt: skip

    @app.get("/api/audit/export")
    def audit_export() -> Response:
        lines = "\n".join(r.to_json_line() for r in need_audit().records())
        return PlainTextResponse(lines + "\n", media_type="application/x-ndjson", headers={
            "Content-Disposition": 'attachment; filename="cairnward-audit.jsonl"'})  # fmt: skip

    @app.get("/api/egress")
    def egress(run: str | None = None, client: str | None = None, env: str | None = None,
               conversation: str | None = None, since: str | None = None,
               format: str = "json") -> Any:  # fmt: skip
        from cairnward.audit import egress_report as er

        try:
            cutoff = er.parse_since(since) if since else None
        except ValueError:
            raise HTTPException(400, "since: use e.g. 24h, 7d or an ISO date") from None
        remote = {n for n, b in cfg.backends.backends.items() if b.location != "local"}
        rows = er.build(need_audit().records(), remote_backends=remote, run=run, client=client,
                        environment=env, conversation=conversation, since=cutoff)  # fmt: skip
        if format == "csv":
            return PlainTextResponse(er.render_csv(rows), media_type="text/csv", headers={
                "Content-Disposition": 'attachment; filename="cairnward-egress.csv"'})  # fmt: skip
        return [r.as_dict() for r in rows]

    # ------------------------------------------------------------------ vault

    from cairnward.store.vault import LockedError, NotFoundError, VaultError

    def need_vault() -> Any:
        if vault is None:
            raise HTTPException(503, "the vault isn't open in this process")
        return vault

    def vault_call(fn: Callable[[], Any]) -> Any:
        try:
            return fn()
        except NotFoundError as exc:
            raise HTTPException(404, str(exc)) from None
        except LockedError as exc:
            raise HTTPException(423, str(exc)) from None
        except VaultError as exc:
            raise HTTPException(400, str(exc)) from None

    def conv_dict(c: Any) -> dict[str, Any]:
        return dataclasses.asdict(c)

    def user_event(action: str, conv_id: str, **details: Any) -> None:
        if audit:
            audit.append(action, actor="user", target=conv_id, conversation_id=conv_id,
                         details={"via": "dashboard", **details})  # fmt: skip

    @app.get("/api/vault")
    def vault_list(env: str | None = None, trash: bool = False, q: str | None = None,
                   folder: str | None = None) -> list[dict[str, Any]]:  # fmt: skip
        v = need_vault()
        if q:
            return [{**conv_dict(c), "snippet": snip} for c, snip in v.search(q, environment=env)]
        convs = v.list_conversations(environment=env, only_deleted=trash, folder_id=folder)
        return [conv_dict(c) for c in convs]

    @app.get("/api/vault/folders")
    def folders() -> list[dict[str, Any]]:
        return [dataclasses.asdict(f) for f in need_vault().list_folders()]

    @app.post("/api/vault/folders")
    def new_folder(body: FolderIn) -> dict[str, str]:
        return {"id": need_vault().create_folder(body.name)}

    @app.get("/api/vault/{conv_id}")
    def vault_show(conv_id: str) -> dict[str, Any]:
        v = need_vault()
        conv = vault_call(lambda: v.conversation(conv_id))
        msgs = vault_call(lambda: v.messages(conv_id))
        return {"conversation": conv_dict(conv),
                "messages": [dataclasses.asdict(m) for m in msgs]}  # fmt: skip

    @app.get("/api/vault/{conv_id}/export")
    def vault_export(conv_id: str, format: str = "md") -> Response:
        from cairnward.vault_cli import to_json, to_markdown

        v = need_vault()
        conv = vault_call(lambda: v.conversation(conv_id))
        msgs = vault_call(lambda: v.messages(conv_id))
        text = to_json(conv, msgs) if format == "json" else to_markdown(conv, msgs)
        user_event("vault.export", conv_id, format=format)
        name = f"conversation-{conv_id[:10]}.{'json' if format == 'json' else 'md'}"
        disposition = f'attachment; filename="{name}"'
        return PlainTextResponse(text, headers={"Content-Disposition": disposition})

    @app.post("/api/vault/{conv_id}/{op}")
    async def vault_op(conv_id: str, op: str, request: Request) -> dict[str, Any]:
        v = need_vault()
        body: dict[str, Any] = {}
        if (await request.body()).strip():
            body = await request.json()
        if op == "delete":
            vault_call(lambda: v.soft_delete(conv_id))
        elif op == "restore":
            vault_call(lambda: v.restore(conv_id))
        elif op == "purge":
            vault_call(lambda: v.purge(conv_id, force=bool(body.get("force"))))
        elif op == "pin":
            vault_call(lambda: v.set_pinned(conv_id, bool(body.get("pinned", True))))
        elif op == "title":
            title = TitleIn.model_validate(body).title
            vault_call(lambda: v.set_title(conv_id, title))
        elif op == "move":
            folder_id = MoveIn.model_validate(body).folder_id
            vault_call(lambda: v.move_to_folder(conv_id, folder_id))
        elif op == "lock":
            secret = PassIn.model_validate(body).passphrase
            await asyncio.to_thread(vault_call, lambda: v.lock(conv_id, secret))
        elif op == "unlock":
            data = PassIn.model_validate(body)
            await asyncio.to_thread(vault_call, lambda: v.unlock(conv_id, data.passphrase,
                                                                 minutes=data.minutes))  # fmt: skip
        elif op == "relock":
            v.relock(conv_id)
        else:
            raise HTTPException(404, f"unknown operation {op}")
        if op in ("delete", "restore", "purge", "lock", "unlock"):
            user_event(f"vault.{op}", conv_id)
        return {"ok": True}

    # ------------------------------------------------------------------ research jobs

    def need_jobs() -> Any:
        if jobs is None:
            raise HTTPException(503, "research jobs aren't available in this process")
        return jobs

    @app.get("/api/jobs")
    def jobs_list() -> list[dict[str, Any]]:
        return [j.summary() for j in reversed(need_jobs().store.all())]

    @app.get("/api/jobs/{job_id}")
    def job_show(job_id: str) -> dict[str, Any]:
        try:
            job = need_jobs().store.load(job_id)
        except (KeyError, OSError):
            raise HTTPException(404, "no such job") from None
        result = need_jobs().store.result_path(job_id)
        return {**job.summary(), "steps": [
            {"name": st.name, "label": st.params.get("title") or st.params.get("question"),
             "status": st.status, "seconds": st.seconds, "error": st.error} for st in job.steps],
            "result": result.read_text(encoding="utf-8") if result.exists() else None}  # fmt: skip

    @app.post("/api/jobs/{job_id}/cancel")
    def job_cancel(job_id: str) -> dict[str, bool]:
        try:
            return {"cancelled": need_jobs().cancel(job_id)}
        except (KeyError, OSError):
            raise HTTPException(404, "no such job") from None

    # ------------------------------------------------------------------ MCP

    @app.get("/api/mcp/servers")
    def mcp_servers() -> list[dict[str, Any]]:
        return [{"name": n, "description": s.description, "version": s.version,
                 "command": s.command, "permissions": s.permissions.model_dump(),
                 "tools": s.tools, "default_tool": s.default_tool,
                 "environments": [e for e, c in cfg.environments.items() if n in c.mcp_servers]}
                for n, s in cfg.mcp.servers.items()]  # fmt: skip

    def mcp_env(server: str, environment: str | None) -> str:
        envs = [e for e, c in cfg.environments.items() if server in c.mcp_servers]
        if server not in cfg.mcp.servers or not envs:
            raise HTTPException(404, f"'{server}' isn't configured or enabled anywhere")
        if environment and environment not in envs:
            raise HTTPException(400, f"'{server}' isn't enabled in '{environment}'")
        return environment or envs[0]

    @app.get("/api/mcp/{server}/tools")
    async def mcp_tools(server: str, environment: str | None = None) -> dict[str, Any]:
        from cairnward.mcp.jsonrpc import McpError

        env = mcp_env(server, environment)
        if mcp is None:
            raise HTTPException(503, "the MCP proxy isn't running")
        try:
            views = await mcp.server_tools(server, env)
        except McpError as exc:
            raise HTTPException(502, str(exc)) from None
        return {"environment": env, "tools": [{
            "name": v.definition["name"], "description": v.definition.get("description", ""),
            "input_schema": v.definition.get("inputSchema"), "hash": v.state.hash,
            "status": v.state.status, "flags": v.state.flags, "offered": v.offered, "why": v.why,
        } for v in views]}  # fmt: skip

    @app.post("/api/mcp/{server}/approve")
    async def mcp_approve(server: str, body: ApproveToolsIn) -> dict[str, Any]:
        env = mcp_env(server, body.environment)
        if mcp is None:
            raise HTTPException(503, "the MCP proxy isn't running")
        views = await mcp.server_tools(server, env)
        try:
            mcp.approve(server, env, body.tools, views)
        except ValueError as exc:
            raise HTTPException(409, str(exc)) from None
        return {"approved": sorted(body.tools)}

    return app
