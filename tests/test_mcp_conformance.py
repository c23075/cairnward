"""MCP conformance with the official SDK (ADR-0015 amendment): the SDK's own client talks
to Cairnward's /mcp, Cairnward's client talks to a server built with the SDK; fields
outside the standard are dropped (and audited); pins made before the switch carry over
only when the definition is unchanged."""

import asyncio
import json
import socket
import sys
from pathlib import Path

import httpx2
import pytest
import uvicorn
from mcp.client.session import ClientSession
from mcp.client.streamable_http import streamable_http_client

from cairnward.audit import AuditLog
from cairnward.config import load_config
from cairnward.config.paths import Paths
from cairnward.mcp.pins import PinStore, tool_hash
from tests.test_mcp import make_hub, mcp_app, setup

SDK_FAKE = Path(__file__).parent / "fakes" / "sdk_mcp_server.py"
unix_only = pytest.mark.skipif(sys.platform == "win32", reason="POSIX process groups")


@pytest.fixture
def paths(tmp_path: Path) -> Paths:
    return Paths(tmp_path / "cfg", tmp_path / "data", tmp_path / "state")


def free_port() -> int:
    s = socket.socket()
    s.bind(("127.0.0.1", 0))
    port: int = s.getsockname()[1]
    s.close()
    return port


@unix_only
async def test_official_sdk_client_against_cairnward(paths: Paths) -> None:
    cfg = setup(paths, privacy='isolation = "off"\nmcp_pinning = "off"\napprovals = "off"')
    with AuditLog.open(paths) as audit:
        hub = make_hub(cfg, audit)
        port = free_port()
        server = uvicorn.Server(uvicorn.Config(mcp_app(hub), host="127.0.0.1", port=port,
                                               log_level="warning"))  # fmt: skip
        task = asyncio.create_task(server.serve())
        try:
            while not server.started:
                await asyncio.sleep(0.05)
            client = httpx2.AsyncClient(headers={"Authorization": "Bearer good"})
            url = f"http://127.0.0.1:{port}/mcp"
            async with (
                client,
                streamable_http_client(url, http_client=client) as (read, write),
                ClientSession(read, write) as session,
            ):
                init = await session.initialize()
                assert init.server_info.name == "cairnward"
                tools = (await session.list_tools()).tools
                by_name = {t.name: t for t in tools}
                assert {"fake__echo", "fake__fetch"} <= set(by_name)
                raw = by_name["fake__fetch"].model_dump(by_alias=True, exclude_unset=True)
                assert "x-vendor-hint" not in json.dumps(raw)  # non-standard: dropped
                got = await session.call_tool("fake__echo", {"text": "conformance"})
                assert got.content[0].text == "conformance"  # type: ignore[union-attr]
        finally:
            server.should_exit = True
            await task
            await hub.aclose()
        dropped = [r for r in audit.records() if r.action == "mcp.nonstandard"]
        assert dropped and dropped[0].details["dropped_fields"] == ["x-vendor-hint"]


@unix_only
async def test_cairnward_client_against_an_sdk_server(paths: Paths) -> None:
    (paths.config_dir / "environments").mkdir(parents=True)
    (paths.config_dir / "environments" / "e.toml").write_text(
        'mcp_servers = ["sdkfake"]\n[privacy]\nisolation = "off"\nmcp_pinning = "off"\n'
        'approvals = "off"\n'
    )
    cmd = json.dumps([sys.executable, str(SDK_FAKE)])
    (paths.config_dir / "mcp.toml").write_text(f"[servers.sdkfake]\ncommand = {cmd}\n")
    cfg = load_config(paths)
    with AuditLog.open(paths) as audit:
        hub = make_hub(cfg, audit)
        try:
            views = await hub.server_tools("sdkfake", "e")
            assert [v.definition["name"] for v in views] == ["shout"]
            assert views[0].definition["annotations"] == {"readOnlyHint": True}
            result = await hub.call("e", "sdkfake__shout", {"text": "hi"}, client="t")
            assert result["content"][0]["text"] == "HI"
            assert result["structuredContent"] == {"shouted": "HI"}
            start = next(r for r in audit.records() if r.action == "mcp.start")
            assert start.details["server_name"] == "sdk-fake"
        finally:
            await hub.aclose()


@unix_only
async def test_pins_from_before_the_sdk_carry_over_only_when_unchanged(paths: Paths) -> None:
    cfg = setup(paths, privacy='isolation = "off"\nmcp_pinning = "on"')
    hub = make_hub(cfg, None)
    try:
        await hub.server_tools("fake", "e")  # start it, to read the raw definitions
        entry = next(iter(hub._servers.values()))
        raw = entry.conn.raw_tools
        # a pin file as the old layer wrote it: hashes of the raw definitions
        old = {"fake": {"tools": {
            "fetch": {"hash": tool_hash(raw["fetch"]), "approved_at": 1},  # unchanged
            "echo": {"hash": tool_hash({**raw["echo"], "description": "old"}), "approved_at": 1},
        }}}  # fmt: skip
        pins_file = paths.data_dir / "mcp_pins.json"
        pins_file.write_text(json.dumps(old))
        hub.pins = PinStore.at(paths.data_dir)
        states = {v.definition["name"]: v.state.status for v in await hub.server_tools("fake", "e")}
        assert states["fetch"] == "approved"  # same definition: carried over
        assert states["echo"] == "changed"  # differs from what was approved: asked again
        assert states["write_note"] == "new"
        saved = json.loads(pins_file.read_text())["fake"]["tools"]
        assert saved["fetch"]["format"] == 2 and saved["fetch"]["hash"] != old["fake"]["tools"][
            "fetch"]["hash"]  # fmt: skip
        assert "format" not in saved["echo"]
    finally:
        await hub.aclose()
