"""srt's ask callback, through Cairnward's Node wrapper (srt_ask.mjs), with the real sandbox.

A fake management socket answers: one local origin allowed, another denied with a reason.
Skipped unless sandbox-runtime is installed as the node package (CI's isolation job).
"""

import json
import os
import subprocess
import sys
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any

import pytest
import uvicorn
from fastapi import FastAPI

from cairnward.config.paths import Paths
from cairnward.sandbox import SandboxSpec, locate, wrap_with_ask

pytestmark = pytest.mark.skipif(not sys.platform.startswith("linux"), reason="Linux only")


class Origin(BaseHTTPRequestHandler):
    def log_message(self, *a: Any) -> None:
        pass

    def do_GET(self) -> None:
        body = b"origin says hi"
        self.send_response(200)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)


def origin() -> ThreadingHTTPServer:
    srv = ThreadingHTTPServer(("127.0.0.1", 0), Origin)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    return srv


PROBE = r"""
import json, sys, urllib.request, urllib.error
out = {}
for name, url in (("allowed", sys.argv[1]), ("denied", sys.argv[2])):
    try:
        out[name] = urllib.request.urlopen(url, timeout=20).read().decode()
    except urllib.error.HTTPError as e:
        out[name] = f"HTTP {e.code}: {e.read().decode(errors='replace')[:300]}"
    except Exception as e:
        out[name] = f"error: {e}"
print("RESULT " + json.dumps(out))
"""


def test_sandbox_asks_cairnward(tmp_path: Path) -> None:
    srt = locate(Paths(tmp_path / "c", tmp_path / "d", tmp_path / "s"))
    if srt is None:
        pytest.skip("sandbox-runtime not installed (cairnward sandbox install / CAIRNWARD_SRT)")
    ok_srv, no_srv = origin(), origin()
    ok_target = f"127.0.0.1:{ok_srv.server_address[1]}"
    asked: list[dict[str, Any]] = []
    app = FastAPI()

    @app.post("/v1/approvals/sandbox")
    def ask(body: dict[str, Any]) -> dict[str, Any]:
        asked.append(body)
        if f"{body['host']}:{body['port']}" == ok_target and body["run_id"] == "run-1":
            return {"allow": True}
        return {"allow": False, "reason": "cairnward: not approved (test)"}

    sock = tmp_path / "m.sock"
    server = uvicorn.Server(uvicorn.Config(app, uds=str(sock), log_level="warning"))
    th = threading.Thread(target=server.run, daemon=True)
    th.start()
    ws = tmp_path / "ws"
    ws.mkdir()
    (tmp_path / "probe.py").write_text(PROBE)
    settings = SandboxSpec(allowed_hosts=["unused.invalid"], allow_write=[ws],
                           deny_read=[]).write(tmp_path / "srt.json")  # fmt: skip
    argv = wrap_with_ask(
        srt, settings,
        [sys.executable, "-I", str(tmp_path / "probe.py"), f"http://{ok_target}/",
         f"http://127.0.0.1:{no_srv.server_address[1]}/"],
        ask_socket=sock, run_id="run-1", tmp_dir=ws,
    )  # fmt: skip
    if argv is None:
        pytest.skip("srt isn't the node package: the ask wrapper can't be used")
    try:
        for _ in range(500):
            if server.started:
                break
            time.sleep(0.01)
        env = {k: os.environ[k] for k in ("PATH", "HOME", "LANG") if k in os.environ}
        res = subprocess.run(argv, capture_output=True, text=True, timeout=120, env=env,
                             cwd=ws, check=False)  # fmt: skip
    finally:
        server.should_exit = True
        th.join(5)
        ok_srv.shutdown()
        no_srv.shutdown()
    line = next((x for x in res.stdout.splitlines() if x.startswith("RESULT ")), None)
    assert line, res.stdout + res.stderr
    got = json.loads(line[7:])
    assert got["allowed"] == "origin says hi"
    assert got["denied"].startswith("HTTP 403"), got["denied"]
    assert {(a["host"], a["port"]) for a in asked} >= {
        ("127.0.0.1", ok_srv.server_address[1]),
        ("127.0.0.1", no_srv.server_address[1]),
    }
