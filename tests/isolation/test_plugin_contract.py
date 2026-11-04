"""The plugin contract end to end (ADR-0017): a real `serve`, the real sandbox, the real egress
proxy and a real local origin. Skipped unless srt is installed."""

import json
import subprocess
import sys
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any

import pytest

from cairnward.plugins.contract import tree_digest
from tests.isolation.harness import MODES, Cairnward

pytestmark = pytest.mark.skipif(not sys.platform.startswith("linux"), reason="Linux only")
PLUGIN = Path(__file__).parent.parent / "fakes" / "plugin_server.py"


SEEN_HOSTS: list[str] = []


class Origin(BaseHTTPRequestHandler):

    def log_message(self, *a: Any) -> None:
        pass

    def do_GET(self) -> None:
        SEEN_HOSTS.append(self.headers.get("Host", ""))
        body = b"from origin"
        self.send_response(200)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)


def installed(harness: Cairnward, origin_port: int, tmp_path: Path) -> Path:
    code = tmp_path / "plugin-code"
    code.mkdir()
    (code / "plugin_server.py").write_text(PLUGIN.read_text())
    manifest = tmp_path / "plugin.toml"
    manifest.write_text(f"""
name = "demo"
version = "1.0"
command = {json.dumps([sys.executable, "-I", str(code / "plugin_server.py")])}
pin_dir = "{code}"
fetch_hosts = ["127.0.0.1:{origin_port}"]
environments = ["coding"]
idle_stop_minutes = 0

[permissions]
read = []
""")
    return manifest


def shim(harness: Cairnward, key: str, *calls: tuple[str, str]) -> list[str]:
    msgs: list[dict[str, Any]] = [
        {"jsonrpc": "2.0", "id": 1, "method": "initialize",
         "params": {"protocolVersion": "2025-06-18", "capabilities": {},
                    "clientInfo": {"name": "test", "version": "1"}}},
        {"jsonrpc": "2.0", "method": "notifications/initialized"},
    ]  # fmt: skip
    for i, (tool, value) in enumerate(calls, 2):
        msgs.append({"jsonrpc": "2.0", "id": i, "method": "tools/call",
                     "params": {"name": f"demo__{tool}",
                                "arguments": {"value": value}}})  # fmt: skip
    res = subprocess.run(
        [sys.executable, "-m", "cairnward", "mcp", "--env", "coding", "--key", key],
        env=harness.env, text=True, timeout=180, capture_output=True, check=False,
        input="".join(json.dumps(m) + "\n" for m in msgs),
    )  # fmt: skip
    replies = [json.loads(x) for x in res.stdout.splitlines()][1:]
    assert len(replies) == len(calls), res.stdout + res.stderr
    return [r["result"]["content"][0]["text"] for r in replies]


@pytest.mark.parametrize("mode", MODES)
def test_plugin_flow(tmp_path: Path, mode: str) -> None:
    """Whatever the environment's isolation, a plugin runs in the sandbox with no network of
    its own, and reaches the world only through the core's services."""
    origin = ThreadingHTTPServer(("127.0.0.1", 0), Origin)
    threading.Thread(target=origin.serve_forever, daemon=True).start()
    port = origin.server_address[1]
    allow = f'network = "allowlist"\nnetwork_allow_hosts = ["127.0.0.1:{port}"]\n'
    privacy = f'preset = "balanced"\napprovals = "off"\nmcp_pinning = "on"\n{allow}'
    harness = Cairnward(tmp_path, mode, privacy=privacy)
    manifest = installed(harness, port, tmp_path)
    from cairnward.sandbox import locate

    if locate(harness.paths) is None:
        pytest.skip("sandbox-runtime not installed")
    assert harness.cw("plugin", "add", str(manifest), "--yes").returncode == 0
    listed = harness.cw("plugin", "list").stdout
    assert "hasn't passed" in listed, listed
    with harness:
        verified = harness.cw("plugin", "verify", "demo", timeout=240)
        assert verified.returncode == 0, verified.stdout + verified.stderr
        assert "passed contract 1" in verified.stdout
        appr = harness.cw("mcp", "approve", "demo", "--yes")
        assert "approved" in appr.stdout, appr.stdout + appr.stderr
        key = harness.cw("client", "add", "tester", check=True).stdout.splitlines()[1]
        fetched, direct, saved, env, inside, outside = shim(
            harness, key,
            ("through_fetch", f"http://127.0.0.1:{port}/"),
            ("direct", f"http://127.0.0.1:{port}/"),
            ("save", "notes/a.md"),
            ("env", ""),
            ("write", str(harness.paths.data_dir / "mcp" / "demo" / "ok.txt")),
            ("write", str(tmp_path / "escape.txt")),
        )  # fmt: skip
        refused = shim(harness, key, ("through_fetch", "http://other.invalid/"))[0]
    assert fetched == "got from origin", fetched
    assert direct.startswith("blocked"), direct
    assert saved.startswith("200"), saved
    assert json.loads(env)["environment"] == "coding" and json.loads(env)["contract"] == "1"
    assert inside == "written" and outside.startswith("blocked")
    assert not (tmp_path / "escape.txt").exists()
    assert refused.startswith("refused 403"), refused
    assert SEEN_HOSTS[-1] == f"127.0.0.1:{port}"
    assert (harness.paths.data_dir / "artifacts" / "plugin" / "demo").exists()
    recs = harness.audit(300)
    start = next(r for r in recs if r["action"] == "mcp.start" and r["target"] == "demo")
    assert start["details"]["sandboxed"] is True
    fetches = [r for r in recs if r["action"] == "plugin.fetch"]
    assert any(r["outcome"] == "ok" and r["details"]["request_digest"] for r in fetches)
    assert any(r.get("decision") == "deny" for r in fetches)
    assert all("from origin" not in json.dumps(r) for r in recs)
    origin.shutdown()


def test_changed_code_stops_the_plugin(tmp_path: Path) -> None:
    origin_port = 9
    harness = Cairnward(tmp_path, "isolated",
                        privacy='preset = "balanced"\napprovals = "off"\n')  # fmt: skip
    manifest = installed(harness, origin_port, tmp_path)
    assert harness.cw("plugin", "add", str(manifest), "--yes").returncode == 0
    with harness:
        assert harness.cw("plugin", "verify", "demo", timeout=240).returncode == 0
        code = tmp_path / "plugin-code"
        (code / "plugin_server.py").write_text(PLUGIN.read_text() + "\n# changed\n")
        assert tree_digest(code)
        listed = harness.cw("plugin", "list").stdout
        assert "changed" in listed, listed
        tools = harness.cw("mcp", "tools", "demo")
        assert "changed" in tools.stdout + tools.stderr
