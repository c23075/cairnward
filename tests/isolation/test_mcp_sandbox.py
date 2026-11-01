"""MCP servers in the real sandbox: manifest limits, and the whole flow with the core
isolated (the outer process starts the server). Skipped unless srt is installed."""

import json
import os
import subprocess
import sys
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any

import pytest

from cairnward.config import load_config
from cairnward.config.paths import Paths
from cairnward.mcp.hub import McpHub
from cairnward.mcp.launch import LocalLauncher
from cairnward.mcp.pins import PinStore
from cairnward.policy import PolicyEngine
from cairnward.sandbox import locate

pytestmark = pytest.mark.skipif(not sys.platform.startswith("linux"), reason="Linux only")
FAKE = Path(__file__).parent.parent / "fakes" / "mcp_server.py"


class Origin(BaseHTTPRequestHandler):
    def log_message(self, *a: Any) -> None:
        pass

    def do_GET(self) -> None:
        body = b"reached"
        self.send_response(200)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)


def write_config(paths: Paths, ws: Path, env: str, extra_main: str = "") -> None:
    (paths.config_dir / "environments").mkdir(parents=True, exist_ok=True)
    (paths.config_dir / "environments" / f"{env}.toml").write_text(
        f'workspace = "{ws}"\nmcp_servers = ["fake"]\n'
    )
    (paths.config_dir / "cairnward.toml").write_text(
        f'{extra_main}[privacy]\npreset = "strict"\nmcp_pinning = "off"\napprovals = "off"\n'
    )
    cmd = json.dumps([sys.executable, "-I", str(FAKE)])
    (paths.config_dir / "mcp.toml").write_text(
        f'[servers.fake]\ncommand = {cmd}\n[servers.fake.permissions]\nwrite = ["{{workspace}}"]\n'
    )


async def test_sandboxed_server_limits(tmp_path: Path) -> None:
    paths = Paths(tmp_path / "cfg", tmp_path / "data", tmp_path / "state")
    srt = locate(paths)
    if srt is None:
        pytest.skip("sandbox-runtime not installed (CAIRNWARD_SRT)")
    ws = tmp_path / "ws"
    ws.mkdir()
    write_config(paths, ws, "e")
    cfg = load_config(paths)
    origin = ThreadingHTTPServer(("127.0.0.1", 0), Origin)
    threading.Thread(target=origin.serve_forever, daemon=True).start()
    hub = McpHub(cfg, PolicyEngine(cfg), LocalLauncher(cfg, srt=srt), PinStore.at(paths.data_dir))
    try:

        async def call(tool: str, **args: str) -> dict[str, Any]:
            return await hub.call("e", f"fake__{tool}", args, client="t")

        ok = await call("write_note", path=str(ws / "n.txt"), text="x")
        assert not ok.get("isError") and (ws / "n.txt").read_text() == "x"
        outside = await call("write_note", path=str(tmp_path / "escape.txt"), text="x")
        assert outside["isError"] and not (tmp_path / "escape.txt").exists()
        # Cairnward's own folders are hidden (an empty, throwaway folder in the sandbox)
        await call("write_note", path=str(paths.config_dir / "evil.toml"), text="x")
        assert not (paths.config_dir / "evil.toml").exists()
        net = await call("fetch", url=f"http://127.0.0.1:{origin.server_address[1]}/")
        assert net["isError"] and "reached" not in json.dumps(net)
    finally:
        await hub.aclose()
        origin.shutdown()


def test_isolated_serve_mcp_flow(tmp_path: Path) -> None:
    """Core isolated: approve with the CLI, call a tool through `cairnward mcp`."""
    env = {**os.environ}
    paths = Paths(tmp_path / "cfg", tmp_path / "data", tmp_path / "state")
    env.update(
        CAIRNWARD_CONFIG_DIR=str(paths.config_dir),
        CAIRNWARD_DATA_DIR=str(paths.data_dir),
        CAIRNWARD_STATE_DIR=str(paths.state_dir),
    )
    if locate(paths) is None:
        pytest.skip("sandbox-runtime not installed (CAIRNWARD_SRT)")
    from tests.isolation.test_isolated_serve import free_port

    gw, dash, eg = free_port(), free_port(), free_port()
    ws = tmp_path / "ws"
    ws.mkdir()
    ports = f"[server]\ngateway_port = {gw}\ndashboard_port = {dash}\negress_port = {eg}\n"
    write_config(paths, ws, "coding", ports)
    pins_on = (paths.config_dir / "cairnward.toml").read_text().replace(
        'mcp_pinning = "off"', 'mcp_pinning = "on"')  # fmt: skip
    (paths.config_dir / "cairnward.toml").write_text(pins_on)
    (paths.config_dir / "backends.toml").write_text(
        '[backends.local-ollama]\ndriver = "ollama"\nurl = "http://127.0.0.1:9"\n'
        '[roles.main]\nbackend = "local-ollama"\nmodel = "m"\n'
        '[roles.fast]\nbackend = "local-ollama"\nmodel = "m"\n'
    )
    cw = [sys.executable, "-m", "cairnward"]
    serve = subprocess.Popen(
        [*cw, "serve"],
        env=env,
        stdin=subprocess.DEVNULL,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        text=True,
    )
    try:
        st = subprocess.run([*cw, "status", "--wait", "60"], env=env, capture_output=True,
                            text=True, check=False)  # fmt: skip
        assert "core isolated" in st.stdout, st.stdout + st.stderr
        tools = subprocess.run([*cw, "mcp", "tools", "fake"], env=env, capture_output=True,
                               text=True, check=False)  # fmt: skip
        assert "[NEW    ] echo" in tools.stdout, tools.stdout + tools.stderr
        appr = subprocess.run([*cw, "mcp", "approve", "fake", "--yes"], env=env,
                              capture_output=True, text=True, check=False)  # fmt: skip
        assert "approved: echo, fetch, write_note" in appr.stdout, appr.stdout + appr.stderr
        key = subprocess.run([*cw, "client", "add", "tester"], env=env, capture_output=True,
                             text=True, check=True).stdout.splitlines()[1]  # fmt: skip
        msgs = [
            {"jsonrpc": "2.0", "id": 1, "method": "initialize",
             "params": {"protocolVersion": "2025-06-18"}},
            {"jsonrpc": "2.0", "method": "notifications/initialized"},
            {"jsonrpc": "2.0", "id": 2, "method": "tools/list"},
            {"jsonrpc": "2.0", "id": 3, "method": "tools/call",
             "params": {"name": "fake__write_note",
                        "arguments": {"path": str(ws / "via-mcp.txt"), "text": "hello"}}},
        ]  # fmt: skip
        shim = subprocess.run(
            [*cw, "mcp", "--env", "coding", "--key", key], env=env, text=True, timeout=120,
            input="".join(json.dumps(m) + "\n" for m in msgs), capture_output=True, check=False,
        )  # fmt: skip
        replies = [json.loads(x) for x in shim.stdout.splitlines()]
        assert len(replies) == 3, shim.stdout + shim.stderr
        assert [t["name"] for t in replies[1]["result"]["tools"]] == [
            "fake__echo", "fake__write_note", "fake__fetch"]  # fmt: skip
        assert replies[2]["result"]["content"][0]["text"] == "written", replies[2]
        assert (ws / "via-mcp.txt").read_text() == "hello"
    finally:
        serve.terminate()
        try:
            serve.wait(20)
        except subprocess.TimeoutExpired:
            serve.kill()
    audit = subprocess.run([*cw, "audit", "log", "--json", "--last", "100"], env=env,
                           capture_output=True, text=True, check=True).stdout  # fmt: skip
    recs = [json.loads(x) for x in audit.splitlines()]
    start = next(r for r in recs if r["action"] == "mcp.start")
    assert start["details"]["sandboxed"] is True
    assert any(r["action"] == "mcp.approve" for r in recs)
    assert any(r["action"] == "tool.call" and r["phase"] == "post" and r["outcome"] == "ok"
               for r in recs)  # fmt: skip
    assert not list((paths.state_dir / "mcp").glob("*.sock"))
