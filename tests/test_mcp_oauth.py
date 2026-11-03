"""OAuth for remote MCP servers: the real MCP SDK against a fake authorization server and a
protected SDK server, all on real local sockets. Discovery, registration, PKCE, refresh; the
core never starts a sign-in; every host is checked by policy; tokens aren't logged."""

import base64
import contextlib
import hashlib
import json
import secrets
import socket
import stat
import threading
import time
from collections.abc import Iterator
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any
from urllib.parse import parse_qs, urlencode, urlsplit

import httpx
import pytest
import uvicorn
from fastapi import FastAPI, Form, Request
from fastapi.responses import JSONResponse, RedirectResponse
from mcp.server.mcpserver import MCPServer
from pydantic import ValidationError

from cairnward.audit import AuditLog
from cairnward.cli import main
from cairnward.config import load_config
from cairnward.config.models import McpServer
from cairnward.config.paths import Paths
from cairnward.mcp import oauth
from cairnward.mcp.hub import McpHub
from cairnward.mcp.jsonrpc import McpError
from cairnward.mcp.launch import LocalLauncher
from cairnward.mcp.pins import PinStore
from cairnward.policy import PolicyEngine


def serve(app: Any) -> tuple[int, Any, threading.Thread]:
    sock = socket.socket()
    sock.bind(("127.0.0.1", 0))
    server = uvicorn.Server(uvicorn.Config(app, log_level="error", lifespan="on"))
    thread = threading.Thread(target=server.run, kwargs={"sockets": [sock]}, daemon=True)
    thread.start()
    while not server.started:
        time.sleep(0.02)
    return sock.getsockname()[1], server, thread


@dataclass
class Auth:
    """The authorization server's state and what it saw."""

    port: int = 0
    resource_port: int = 0
    clients: dict[str, dict[str, Any]] = field(default_factory=dict)
    codes: dict[str, dict[str, str]] = field(default_factory=dict)
    access: set[str] = field(default_factory=set)
    refresh: set[str] = field(default_factory=set)
    grants: list[str] = field(default_factory=list)
    token_requests: list[dict[str, str]] = field(default_factory=list)
    expires_in: int = 3600
    refuse_refresh: bool = False
    deny_sign_in: bool = False

    def issue(self) -> dict[str, Any]:
        access, refresh = secrets.token_urlsafe(16), secrets.token_urlsafe(16)
        self.access.add(access)
        self.refresh.add(refresh)
        return {"access_token": access, "token_type": "Bearer", "expires_in": self.expires_in,
                "refresh_token": refresh, "scope": "notes"}  # fmt: skip

    def expire_all(self) -> None:
        self.access.clear()


def auth_app(a: Auth) -> FastAPI:
    app = FastAPI()

    @app.get("/.well-known/oauth-authorization-server")
    def metadata() -> dict[str, Any]:
        base = f"http://127.0.0.1:{a.port}"
        return {"issuer": base, "authorization_endpoint": f"{base}/authorize",
                "token_endpoint": f"{base}/token", "registration_endpoint": f"{base}/register",
                "response_types_supported": ["code"],
                "grant_types_supported": ["authorization_code", "refresh_token"],
                "code_challenge_methods_supported": ["S256"],
                "token_endpoint_auth_methods_supported": ["none"]}  # fmt: skip

    @app.post("/register", status_code=201)
    async def register(request: Request) -> dict[str, Any]:
        body = await request.json()
        cid = secrets.token_urlsafe(8)
        a.clients[cid] = body
        return {**body, "client_id": cid, "client_id_issued_at": int(time.time())}

    @app.get("/authorize")
    def authorize(client_id: str, redirect_uri: str, state: str, code_challenge: str,
                  code_challenge_method: str) -> RedirectResponse:  # fmt: skip
        assert code_challenge_method == "S256"
        assert redirect_uri in a.clients[client_id]["redirect_uris"]
        if a.deny_sign_in:
            query = urlencode({"error": "access_denied", "error_description": "you said no",
                               "state": state})  # fmt: skip
            return RedirectResponse(f"{redirect_uri}?{query}", 302)
        code = secrets.token_urlsafe(12)
        a.codes[code] = {"challenge": code_challenge, "client": client_id,
                         "redirect": redirect_uri}  # fmt: skip
        return RedirectResponse(f"{redirect_uri}?{urlencode({'code': code, 'state': state})}", 302)

    @app.post("/token")
    def token(grant_type: str = Form(), code: str | None = Form(None),
              code_verifier: str | None = Form(None), refresh_token: str | None = Form(None),
              client_id: str | None = Form(None),
              redirect_uri: str | None = Form(None)) -> JSONResponse:  # fmt: skip
        a.grants.append(grant_type)
        if grant_type == "authorization_code":
            saved = a.codes.pop(code or "", None)  # one use
            if saved is None or client_id != saved["client"] or redirect_uri != saved["redirect"]:
                return JSONResponse({"error": "invalid_grant"}, 400)
            digest = hashlib.sha256((code_verifier or "").encode()).digest()
            want = base64.urlsafe_b64encode(digest).rstrip(b"=").decode()
            if want != saved["challenge"]:  # PKCE
                return JSONResponse({"error": "invalid_grant", "error_description": "pkce"}, 400)
            return JSONResponse(a.issue())
        if a.refuse_refresh or refresh_token not in a.refresh:
            return JSONResponse({"error": "invalid_grant"}, 400)
        a.refresh.discard(refresh_token)  # rotated: the old one stops working
        return JSONResponse(a.issue())

    return app


@pytest.fixture
def servers() -> Iterator[Auth]:
    a = Auth()
    tool = MCPServer("remote-notes")

    @tool.tool()
    def shout(text: str) -> str:
        """Upper-case some text."""
        return text.upper()

    inner = tool.streamable_http_app()

    async def resource(scope: Any, receive: Any, send: Any) -> None:
        if scope["type"] == "http":
            path = scope["path"]
            if path.startswith("/.well-known/oauth-protected-resource"):
                body = json.dumps({"resource": f"http://127.0.0.1:{a.resource_port}/mcp",
                                   "authorization_servers": [f"http://127.0.0.1:{a.port}"],
                                   "scopes_supported": ["notes"]}).encode()  # fmt: skip
                await send({"type": "http.response.start", "status": 200,
                            "headers": [(b"content-type", b"application/json")]})  # fmt: skip
                await send({"type": "http.response.body", "body": body})
                return
            auth = dict(scope["headers"]).get(b"authorization", b"").decode()
            if auth.removeprefix("Bearer ") not in a.access:
                meta = (f'Bearer resource_metadata="http://127.0.0.1:{a.resource_port}'
                        '/.well-known/oauth-protected-resource/mcp"')  # fmt: skip
                await send({"type": "http.response.start", "status": 401,
                            "headers": [(b"www-authenticate", meta.encode())]})  # fmt: skip
                await send({"type": "http.response.body", "body": b""})
                return
        await inner(scope, receive, send)

    a.port, s1, t1 = serve(auth_app(a))
    a.resource_port, s2, t2 = serve(resource)
    yield a
    for server, thread in ((s1, t1), (s2, t2)):
        server.should_exit = True
        thread.join(5)


def make(tmp_path: Path, a: Auth, network: str = "allowlist", *,
         allow_auth_host: bool = True) -> Paths:  # fmt: skip
    p = Paths(tmp_path / "cfg", tmp_path / "data", tmp_path / "state")
    (p.config_dir / "environments").mkdir(parents=True)
    (p.config_dir / "mcp.toml").write_text(
        f'[servers.notes]\nurl = "http://127.0.0.1:{a.resource_port}/mcp"\nversion = "1.0"\n'
        'auth = "oauth"\noauth_scope = "notes"\n'
    )
    hosts = [f"127.0.0.1:{a.resource_port}"] + ([f"127.0.0.1:{a.port}"] if allow_auth_host else [])
    (p.config_dir / "environments" / "work.toml").write_text(
        f'mcp_servers = ["notes"]\n[privacy]\nisolation = "off"\nnetwork = "{network}"\n'
        f"network_allow_hosts = {json.dumps(hosts)}\n"
        'approvals = "off"\nmcp_pinning = "off"\n'
    )
    return p


async def browser(url: str) -> None:
    """What a person's browser does: follow the redirect back to the local listener."""
    async with httpx.AsyncClient(follow_redirects=True, trust_env=False) as c:
        await c.get(url)


async def sign_in(paths: Paths, a: Auth) -> list[str]:
    return await oauth.login(paths, "notes", f"http://127.0.0.1:{a.resource_port}/mcp", "notes",
                             headers={}, proxy=None, open_url=browser, timeout_s=20)  # fmt: skip


def hub_for(paths: Paths, audit: AuditLog) -> McpHub:
    cfg = load_config(paths)
    return McpHub(cfg, PolicyEngine(cfg, audit=audit), LocalLauncher(cfg, srt=None),
                  PinStore.at(paths.data_dir), audit)  # fmt: skip


async def test_sign_in_then_the_core_uses_the_tokens(tmp_path: Path, servers: Auth) -> None:
    paths = make(tmp_path, servers)
    assert await sign_in(paths, servers) == ["shout"]
    assert servers.grants == ["authorization_code"] and len(servers.clients) == 1
    (client,) = servers.clients.values()
    assert client["token_endpoint_auth_method"] == "none"
    assert client["redirect_uris"][0].startswith("http://127.0.0.1:")
    token_path = oauth.token_file(paths, "notes")
    assert stat.S_IMODE(token_path.stat().st_mode) == 0o600
    stored = oauth.FileTokenStorage(token_path).status()
    assert stored == {"signed_in": True, "can_refresh": True, "scope": "notes"}
    with AuditLog.open(paths) as audit:
        hub = hub_for(paths, audit)
        try:
            tools, problems = await hub.list_tools("work")
            assert not problems and [t["name"] for t in tools] == ["notes__shout"]
            out = await hub.call("work", "notes__shout", {"text": "hi"}, client="jan")
            assert out["content"][0]["text"] == "HI"
        finally:
            await hub.aclose()
        text = str([r.to_json_line() for r in audit.records()])
    secret = json.loads(token_path.read_text())["tokens"]
    assert secret["access_token"] not in text and secret["refresh_token"] not in text


async def test_an_expired_token_is_refreshed_without_a_browser(tmp_path: Path,
                                                               servers: Auth) -> None:  # fmt: skip
    paths = make(tmp_path, servers)
    await sign_in(paths, servers)
    token_path = oauth.token_file(paths, "notes")
    before = json.loads(token_path.read_text())["tokens"]["refresh_token"]
    servers.expire_all()
    with AuditLog.open(paths) as audit:
        hub = hub_for(paths, audit)
        try:
            tools, problems = await hub.list_tools("work")
        finally:
            await hub.aclose()
    assert not problems and tools and "refresh_token" in servers.grants
    assert json.loads(token_path.read_text())["tokens"]["refresh_token"] != before  # saved


async def test_the_core_never_signs_in(tmp_path: Path, servers: Auth) -> None:
    paths = make(tmp_path, servers)
    with AuditLog.open(paths) as audit:
        hub = hub_for(paths, audit)
        try:
            with pytest.raises(McpError, match="mcp login notes"):
                await hub._server("notes", "work")
        finally:
            await hub.aclose()
    assert "authorization_code" not in servers.grants and not servers.codes


async def test_a_refused_refresh_asks_for_a_new_sign_in(tmp_path: Path, servers: Auth) -> None:
    paths = make(tmp_path, servers)
    await sign_in(paths, servers)
    servers.expire_all()
    servers.refuse_refresh = True
    with AuditLog.open(paths) as audit:
        hub = hub_for(paths, audit)
        try:
            with pytest.raises(McpError, match="mcp login notes"):
                await hub._server("notes", "work")
        finally:
            await hub.aclose()


async def test_policy_checks_the_sign_in_server_too(tmp_path: Path, servers: Auth) -> None:
    """Allowed to reach the tool server doesn't mean allowed to reach anything it names."""
    paths = make(tmp_path, servers)
    await sign_in(paths, servers)
    servers.expire_all()  # so the core has to talk to the token endpoint
    closed = make(tmp_path / "closed", servers, allow_auth_host=False)
    oauth.token_file(closed, "notes").parent.mkdir(parents=True)
    oauth.token_file(closed, "notes").write_text(oauth.token_file(paths, "notes").read_text())
    with AuditLog.open(closed) as audit:
        hub = hub_for(closed, audit)
        try:
            tools, problems = await hub.list_tools("work")
        finally:
            await hub.aclose()
        denied = [r for r in audit.records() if r.action == "net.connect" and r.decision == "deny"]
    assert not tools and problems
    assert any(str(servers.port) in str(r.target) for r in denied)
    assert "refresh_token" not in servers.grants


async def test_a_denied_sign_in_stores_nothing_usable(tmp_path: Path, servers: Auth) -> None:
    paths = make(tmp_path, servers)
    servers.deny_sign_in = True
    with pytest.raises(BaseException) as err:  # the SDK raises it inside an exception group
        await sign_in(paths, servers)
    assert "you said no" in repr(err.value) or "Sign-in failed" in repr(err.value) or (
        any("you said no" in repr(e) for e in getattr(err.value, "exceptions", [])))  # fmt: skip
    assert oauth.FileTokenStorage(oauth.token_file(paths, "notes")).status()["signed_in"] is False


async def test_callback_ignores_other_paths_and_wrong_requests() -> None:
    cb = oauth._Callback()
    redirect = await cb.start()
    try:
        async with httpx.AsyncClient(trust_env=False) as c:
            await c.get(f"http://127.0.0.1:{cb.port}/elsewhere?code=stolen")
            assert cb.result is not None and not cb.result.done()  # a guess at the path fails
            await c.get(f"{redirect}?code=real&state=s1")
        got = await cb.wait(2)
        assert got.code == "real" and got.state == "s1"
    finally:
        await cb.close()


def test_settings_validation() -> None:
    with pytest.raises(ValidationError, match="remote servers"):
        McpServer(command=["x"], auth="oauth")
    with pytest.raises(ValidationError, match="Authorization"):
        McpServer(url="https://mcp.example.org/x", auth="oauth",
                  headers={"Authorization": "Bearer ${env:X}"})  # fmt: skip


def test_cli_login_and_logout(tmp_path: Path, servers: Auth, monkeypatch: pytest.MonkeyPatch,
                              capsys: pytest.CaptureFixture[str]) -> None:  # fmt: skip
    """Everything but the running server's egress session, which gets its own proxy=None."""
    from cairnward.run import runner

    paths = make(tmp_path, servers)
    monkeypatch.setenv("CAIRNWARD_DATA_DIR", str(paths.data_dir))
    monkeypatch.setenv("CAIRNWARD_STATE_DIR", str(paths.state_dir))
    sessions: list[tuple[str, str]] = []

    @contextlib.contextmanager
    def session(cfg: Any, env: str, program: str) -> Iterator[None]:
        sessions.append((env, program))
        yield None

    monkeypatch.setattr(runner, "egress_session", session)

    def open_in_browser(url: str) -> bool:
        assert parse_qs(urlsplit(url).query)["code_challenge_method"] == ["S256"]
        threading.Thread(target=httpx.get, args=(url,), kwargs={"follow_redirects": True,
                         "trust_env": False}, daemon=True).start()  # fmt: skip
        return True

    monkeypatch.setattr("webbrowser.open", open_in_browser)
    base = ["--config-dir", str(paths.config_dir), "mcp"]
    assert main([*base, "login", "notes"]) == 0
    out = capsys.readouterr().out
    assert "signed in to notes: 1 tool(s)" in out and sessions == [("work", "mcp-login")]
    token = json.loads(oauth.token_file(paths, "notes").read_text())["tokens"]["access_token"]
    assert token not in out
    assert main([*base, "logout", "notes"]) == 0
    assert not oauth.token_file(paths, "notes").exists()
    assert main([*base, "logout", "notes"]) == 0
    assert "not signed in" in capsys.readouterr().out
    assert main([*base, "login", "missing"]) == 1
