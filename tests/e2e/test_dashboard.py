"""The dashboard in a real browser (Chromium via Node Playwright), against `serve`.

Skipped unless node can load playwright and axe-core (`npm ci` in tests/e2e, or NODE_PATH
pointing at an install)."""

import json
import os
import shutil
import socket
import subprocess
import sys
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any

import httpx
import pytest

from cairnward import replication
from cairnward.config import load_config
from cairnward.config.paths import Paths

HERE = Path(__file__).parent


def node_playwright() -> bool:
    node = shutil.which("node")
    if node is None:
        return False
    res = subprocess.run([node, "-e", "require('playwright'); require('axe-core')"],
                         capture_output=True, check=False,
                         cwd=HERE)  # fmt: skip
    return res.returncode == 0


pytestmark = pytest.mark.skipif(
    sys.platform == "win32" or not node_playwright(), reason="needs node + playwright + axe-core"
)


def free_port() -> int:
    s = socket.socket()
    s.bind(("127.0.0.1", 0))
    port: int = s.getsockname()[1]
    s.close()
    return port


class FakeOllama(BaseHTTPRequestHandler):
    def log_message(self, *a: Any) -> None:
        pass

    def do_GET(self) -> None:
        body = json.dumps({"models": []}).encode()
        self.send_response(200)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_POST(self) -> None:
        req = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
        body = json.dumps(
            {
                "done": True,
                "done_reason": "stop",
                "prompt_eval_count": 3,
                "eval_count": 2,
                "model": req["model"],
                "message": {"role": "assistant", "content": "noted"},
            }
        ).encode()
        self.send_response(200)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)


def test_dashboard_in_a_browser(tmp_path: Path) -> None:
    paths = Paths(tmp_path / "cfg", tmp_path / "data", tmp_path / "state")
    env = {
        **os.environ,
        "CAIRNWARD_CONFIG_DIR": str(paths.config_dir),
        "CAIRNWARD_DATA_DIR": str(paths.data_dir),
        "CAIRNWARD_STATE_DIR": str(paths.state_dir),
    }
    gw, dash, eg = free_port(), free_port(), free_port()
    srv = ThreadingHTTPServer(("127.0.0.1", 0), FakeOllama)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    paths.config_dir.mkdir(parents=True)
    (paths.config_dir / "cairnward.toml").write_text(
        f"[server]\ngateway_port = {gw}\ndashboard_port = {dash}\negress_port = {eg}\n"
        '[privacy]\npreset = "balanced"\nisolation = "off"\n'
    )
    (paths.config_dir / "backends.toml").write_text(
        f'[backends.local-ollama]\ndriver = "ollama"\nurl = "http://127.0.0.1:{srv.server_address[1]}"\n'
        '[roles.main]\nbackend = "local-ollama"\nmodel = "m"\n'
        '[roles.fast]\nbackend = "local-ollama"\nmodel = "m"\n'
    )
    replication.init(load_config(paths), tmp_path / "shared", name="laptop", mode="on_demand")
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
        st = subprocess.run([*cw, "status", "--wait", "30"], env=env, capture_output=True,
                            text=True, check=False)  # fmt: skip
        assert "running" in st.stdout, st.stdout + st.stderr
        key = subprocess.run([*cw, "client", "add", "e2e"], env=env, capture_output=True,
                             text=True, check=True).stdout.splitlines()[1]  # fmt: skip
        evil = "<img src=x onerror=window.pwned=1> hello from the vault"
        r = httpx.post(f"http://127.0.0.1:{gw}/v1/chat/completions", timeout=30,
                       headers={"Authorization": f"Bearer {key}"},
                       json={"model": "cw/coding:low",
                             "messages": [{"role": "user", "content": evil}]})  # fmt: skip
        assert r.status_code == 200, r.text
        # the client socket for containers reaches the same gateway (FR-ISO-4)
        csock = paths.state_dir / "clients" / "gateway.sock"
        assert oct(csock.stat().st_mode)[-3:] == "600"
        with httpx.Client(transport=httpx.HTTPTransport(uds=str(csock)), timeout=10) as uc:
            models = uc.get(f"http://127.0.0.1:{gw}/v1/models",
                            headers={"Authorization": f"Bearer {key}"})  # fmt: skip
            assert models.status_code == 200 and models.json()["data"]
        # an approval waiting in the inbox (asked as the egress process would)
        asked: list[Any] = []
        sock = paths.state_dir / "cairnward.sock"

        def ask() -> None:
            with httpx.Client(transport=httpx.HTTPTransport(uds=str(sock)), timeout=120) as c:
                asked.append(
                    c.post(
                        "http://cairnward/v1/approvals/ask",
                        json={
                            "kind": "net.connect",
                            "target": "pypi.org:443",
                            "risk": "medium",
                            "reason": "network = ask",
                            "environment": "coding",
                            "client": "e2e",
                            "initiated_by": "model",
                            "provenance": ["output of web_fetch"],
                        },
                    ).json()
                )

        t = threading.Thread(target=ask)
        t.start()
        from cairnward.artifacts import ArtifactStore

        ArtifactStore(paths.data_dir).write(
            "job", "e2e000000001", "page.html",
            "<h1>Report</h1><script>parent.pwned2 = 1; window.top.pwned2 = 1</script>"
            "<img src='https://example.com/beacon.png'>",
        )  # fmt: skip
        link = subprocess.run([*cw, "dashboard", "--no-open"], env=env, capture_output=True,
                              text=True, check=True).stdout.splitlines()[1]  # fmt: skip
        node = subprocess.run([shutil.which("node") or "node", str(HERE / "dashboard.mjs"), link],
                              capture_output=True, text=True, timeout=180, check=False)  # fmt: skip
        line = next((x for x in node.stdout.splitlines() if x.startswith("RESULT ")), None)
        assert line, node.stdout + node.stderr
        out = json.loads(line[7:])
        t.join(10)
    finally:
        serve.terminate()
        try:
            serve.wait(20)
        except subprocess.TimeoutExpired:
            serve.kill()
        srv.shutdown()
    # no script errors and no CSP violations (the browser logs the expected 423 Locked)
    # the sandboxed preview doing its job: its script and its beacon image are blocked
    expected = ("status of 423", "status of 409", "Blocked script execution", "beacon.png")
    assert [e for e in out["errors"] if not any(x in e for x in expected)] == []
    assert out["previewSandbox"] == "" and out["pwned2"] is False
    assert out["previewText"] == "Report"
    assert out["landed"] == "/"  # the token left the address bar
    assert out["badge"].startswith("Privacy: Balanced")
    assert out["firstTab"] == "Skip to content"
    assert "pypi.org:443" in out["approvalTitle"] and out["provenanceShown"]
    assert asked and asked[0]["granted"] is True and asked[0]["by"] == "user (dashboard)"
    assert out["rows"] == 1 and out["messageText"].endswith(evil) and out["pwned"] is False
    assert out["locked"] is True
    assert out["h1_settings"] == "Settings" and out["h1_egress"] == "Egress report"
    assert out["h1_jobs"] == "Research jobs"
    assert out["a11y_settings"] == [] and out["a11y_vault"] == [] and out["a11y_audit"] == []
    assert out["h1_backups"] == "Backups"
    assert "laptop (this device)" in out["otherDevices"] and "cw-sync-" not in out["otherDevices"]
    assert "approvals = off" in out["weakenWarning"] and "approvals" in out["savedBanner"]
    toml = (paths.config_dir / "cairnward.toml").read_text()
    assert 'network = "off"' in toml and 'approvals = "off"' in toml
    assert out["axe"] == {}, json.dumps(out["axe"], indent=1)
    assert out["axeDialogs"] == {}, json.dumps(out["axeDialogs"], indent=1)
    assert out["dialogName"] == "Every kind of field"
    assert out["focusInDialog"] is True and out["focusStaysIn"] is True
    assert out["focusReturned"] is True
