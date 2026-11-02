"""Helper programs Cairnward starts (rclone, restic) get a short egress session against a
real `serve`: their traffic follows the environment's network policy and shows in the
audit log under the helper's name."""

import sys
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any

import httpx
import pytest

from cairnward.config import load_config
from cairnward.run.runner import RunError, egress_session
from tests.isolation.harness import Cairnward

pytestmark = pytest.mark.skipif(sys.platform == "win32", reason="Unix sockets")


class Origin(BaseHTTPRequestHandler):
    def log_message(self, *a: Any) -> None:
        pass

    def do_GET(self) -> None:
        self.send_response(200)
        self.send_header("Content-Length", "2")
        self.end_headers()
        self.wfile.write(b"ok")


def test_helper_traffic_goes_through_egress(tmp_path: Path) -> None:
    origin = ThreadingHTTPServer(("127.0.0.1", 0), Origin)
    threading.Thread(target=origin.serve_forever, daemon=True).start()
    port = origin.server_address[1]
    cw = Cairnward(tmp_path, "off", privacy='preset = "strict"\nnetwork = "allowlist"\n'
                   f'network_allow_hosts = ["127.0.0.1:{port}"]\n')  # fmt: skip
    cfg = load_config(cw.paths)
    try:
        with (
            pytest.raises(RunError, match="isn't running"),
            egress_session(cfg, "coding", "restic"),
        ):
            pass
        with cw:
            with (
                egress_session(cfg, "coding", "restic") as proxy,
                httpx.Client(proxy=proxy, trust_env=False) as c,
            ):
                assert c.get(f"http://127.0.0.1:{port}/").text == "ok"
                refused = c.get(f"http://127.0.0.1:{port + 1}/")  # not on the list
                assert refused.status_code == 403
            # the session ended with the block: its token no longer works
            with httpx.Client(proxy=proxy, trust_env=False) as c:
                assert c.get(f"http://127.0.0.1:{port}/").status_code == 407
            recs = cw.audit()
    finally:
        origin.shutdown()
    connects = [r for r in recs if r["action"] == "net.connect" and r.get("client") == "restic"]
    assert {r["decision"] for r in connects if r["phase"] == "pre"} >= {"allow"}
    assert any(r["decision"] == "deny" for r in connects)
    assert any(r["action"] == "run.start" and r.get("client") == "restic" for r in recs)


MCP_SERVER = r"""
import sys
import uvicorn
from mcp.server.mcpserver import MCPServer

srv = MCPServer("remote-notes")


@srv.tool()
def shout(text: str) -> str:
    "Upper-case some text."
    return text.upper()


uvicorn.run(srv.streamable_http_app(), host="127.0.0.1", port=int(sys.argv[1]),
            log_level="error")
"""


def test_remote_mcp_server_through_egress(tmp_path: Path) -> None:
    """A remote MCP server (url in mcp.toml) under a real serve: reached through egress
    in the environment, so the egress records show it."""
    import socket
    import subprocess
    import time

    from tests.isolation.harness import free_port

    port = free_port()
    script = tmp_path / "remote_mcp.py"
    script.write_text(MCP_SERVER)
    remote = subprocess.Popen([sys.executable, str(script), str(port)])
    try:
        deadline = time.monotonic() + 30
        while time.monotonic() < deadline:
            try:
                socket.create_connection(("127.0.0.1", port), timeout=1).close()
                break
            except OSError:
                time.sleep(0.1)
        cw = Cairnward(tmp_path, "off", privacy='preset = "strict"\n')
        (cw.paths.config_dir / "mcp.toml").write_text(
            f'[servers.notes]\nurl = "http://127.0.0.1:{port}/mcp"\n'
        )
        (cw.paths.config_dir / "environments" / "work.toml").write_text(
            'mcp_servers = ["notes"]\n[privacy]\nnetwork = "allowlist"\n'
            f'network_allow_hosts = ["127.0.0.1:{port}"]\n'
        )
        with cw:
            listed = cw.cw("mcp", "list")
            assert "remote  : http://127.0.0.1" in listed.stdout, listed.stderr
            tools = cw.cw("mcp", "--env", "work", "tools", "notes")
            assert "shout" in tools.stdout, (
                tools.stdout
                + tools.stderr
                + str([r for r in cw.audit() if r["action"] in ("net.connect", "mcp.start")])
            )
            recs = cw.audit()
    finally:
        remote.terminate()
        remote.wait(10)
    start = [r for r in recs if r["action"] == "mcp.start" and r.get("outcome") == "ok"]
    assert start and start[0]["details"]["via_egress"]
    egress = [r for r in recs if r["action"] == "net.connect" and r.get("phase") == "pre"
              and str(port) in str(r.get("target"))]  # fmt: skip
    assert egress and all(r["decision"] == "allow" for r in egress)
