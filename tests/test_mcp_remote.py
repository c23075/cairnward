"""Remote MCP servers (streamable HTTP, ADR-0015 amendment): a real SDK server on this
machine, reached with the bearer token from an environment variable; its host is checked
like any connection in the environment; tools are pinned like a local server's; settings
refuse literal secrets, credentials in the URL and plain http to other machines."""

import socket
import threading
import time
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import pytest
import uvicorn
from mcp.server.mcpserver import MCPServer
from pydantic import ValidationError

from cairnward.audit import AuditLog
from cairnward.config import load_config
from cairnward.config.models import McpServer
from cairnward.config.paths import Paths
from cairnward.mcp.hub import McpHub
from cairnward.mcp.jsonrpc import McpError
from cairnward.mcp.launch import LocalLauncher
from cairnward.mcp.pins import PinStore
from cairnward.policy import PolicyEngine

TOKEN = "test-token-123"


@pytest.fixture(scope="module")
def remote() -> Iterator[tuple[int, list[str]]]:
    seen: list[str] = []
    srv = MCPServer("remote-notes")

    @srv.tool()
    def shout(text: str) -> str:
        """Upper-case some text."""
        return text.upper()

    inner = srv.streamable_http_app()

    async def app(scope: Any, receive: Any, send: Any) -> None:
        if scope["type"] == "http":
            auth = dict(scope["headers"]).get(b"authorization", b"").decode()
            seen.append(auth)
            if auth != f"Bearer {TOKEN}":
                await send({"type": "http.response.start", "status": 401, "headers": []})
                await send({"type": "http.response.body", "body": b""})
                return
        await inner(scope, receive, send)

    sock = socket.socket()
    sock.bind(("127.0.0.1", 0))
    port = sock.getsockname()[1]
    server = uvicorn.Server(uvicorn.Config(app, log_level="error", lifespan="on"))
    t = threading.Thread(target=server.run, kwargs={"sockets": [sock]}, daemon=True)
    t.start()
    while not server.started:
        time.sleep(0.02)
    yield port, seen
    server.should_exit = True
    t.join(5)


def make(tmp_path: Path, port: int, network: str) -> Paths:
    p = Paths(tmp_path / "cfg", tmp_path / "data", tmp_path / "state")
    (p.config_dir / "environments").mkdir(parents=True)
    (p.config_dir / "mcp.toml").write_text(
        f'[servers.notes]\nurl = "http://127.0.0.1:{port}/mcp"\nversion = "1.0"\n'
        '[servers.notes.headers]\nAuthorization = "Bearer ${env:NOTES_TOKEN}"\n'
    )
    (p.config_dir / "environments" / "work.toml").write_text(
        f'mcp_servers = ["notes"]\n[privacy]\nisolation = "off"\nnetwork = "{network}"\n'
        f'network_allow_hosts = ["127.0.0.1:{port}"]\napprovals = "off"\nmcp_pinning = "off"\n'
    )
    return p


def hub_for(paths: Paths, audit: AuditLog) -> McpHub:
    cfg = load_config(paths)
    return McpHub(cfg, PolicyEngine(cfg, audit=audit), LocalLauncher(cfg, srt=None),
                  PinStore.at(paths.data_dir), audit)  # fmt: skip


async def test_remote_server_tools(tmp_path: Path, remote: tuple[int, list[str]],
                                   monkeypatch: pytest.MonkeyPatch) -> None:  # fmt: skip
    port, seen = remote
    paths = make(tmp_path, port, "allowlist")
    with AuditLog.open(paths) as audit:
        hub = hub_for(paths, audit)
        try:
            with pytest.raises(McpError, match="NOTES_TOKEN isn't set"):
                await hub._server("notes", "work")
            monkeypatch.setenv("NOTES_TOKEN", TOKEN)
            tools, problems = await hub.list_tools("work")
            assert not problems and [t["name"] for t in tools] == ["notes__shout"]
            out = await hub.call("work", "notes__shout", {"text": "hi"}, client="jan")
            assert out["content"][0]["text"] == "HI"
            assert seen and all(s == f"Bearer {TOKEN}" for s in seen)
        finally:
            await hub.aclose()
        recs = audit.records()
        start = [r for r in recs if r.action == "mcp.start" and r.outcome == "ok"]
        assert start and start[0].details["remote"] and start[0].details["server_name"]
        assert start[0].details["host"] == "127.0.0.1"
        assert TOKEN not in str([r.details for r in recs])


async def test_remote_host_follows_the_environment(
    tmp_path: Path, remote: tuple[int, list[str]], monkeypatch: pytest.MonkeyPatch
) -> None:
    port, seen = remote
    monkeypatch.setenv("NOTES_TOKEN", TOKEN)
    paths = make(tmp_path, port, "off")
    n = len(seen)
    with AuditLog.open(paths) as audit:
        hub = hub_for(paths, audit)
        try:
            tools, problems = await hub.list_tools("work")
        finally:
            await hub.aclose()
        recs = audit.records()
    assert not tools and "refused" in problems[0] and len(seen) == n  # nothing was sent
    assert any(r.action == "net.connect" and r.decision == "deny" for r in recs)


@pytest.mark.parametrize(("fields", "error"), [
    ({"url": "https://mcp.example.org/mcp", "command": ["x"]}, "either command"),
    ({}, "either command"),
    ({"url": "http://mcp.example.org/mcp"}, "needs https"),
    ({"url": "https://u:p@mcp.example.org/mcp"}, "not in the url"),
    ({"url": "https://mcp.example.org/mcp", "headers": {"Authorization": "Bearer abc"}},
     "looks like a secret"),
    ({"url": "https://mcp.example.org/mcp", "env": {"A": "b"}}, "for local servers"),
    ({"command": ["x"], "headers": {"X-Team": "a"}}, "for remote servers"),
])  # fmt: skip
def test_remote_settings_checks(fields: dict[str, Any], error: str) -> None:
    with pytest.raises(ValidationError, match=error):
        McpServer(**fields)


def test_remote_settings_accepted() -> None:
    McpServer(url="https://mcp.example.org/mcp",
              headers={"Authorization": "Bearer ${env:TOKEN}", "X-Team": "lab"})  # fmt: skip
    McpServer(url="http://localhost:8080/mcp")
