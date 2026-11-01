"""End to end with the real sandbox: isolated `serve` + `cairnward run` (Linux, srt installed).

Skipped unless sandbox-runtime is available (CI's isolation job installs it).
"""

import json
import os
import socket
import subprocess
import sys
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any

import pytest

from cairnward.config.paths import Paths
from cairnward.sandbox import locate

pytestmark = pytest.mark.skipif(not sys.platform.startswith("linux"), reason="Linux only")


def free_port() -> int:
    s = socket.socket()
    s.bind(("127.0.0.1", 0))
    port: int = s.getsockname()[1]
    s.close()
    return port


class FakeOllama(BaseHTTPRequestHandler):
    def log_message(self, *a: Any) -> None:
        pass

    def do_POST(self) -> None:
        req = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
        body = json.dumps(
            {
                "done": True,
                "done_reason": "stop",
                "prompt_eval_count": 3,
                "eval_count": 2,
                "model": req["model"],
                "message": {"role": "assistant", "content": "ok from model"},
            }
        ).encode()
        self.send_response(200)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)


AGENT = r"""
import json, os, socket, sys, urllib.request
base, key = os.environ["OPENAI_BASE_URL"], os.environ["OPENAI_API_KEY"]
req = urllib.request.Request(base + "/chat/completions", method="POST",
    data=json.dumps({"model": os.environ["CAIRNWARD_MODEL"],
                     "messages": [{"role": "user", "content": "hi"}]}).encode(),
    headers={"Authorization": "Bearer " + key, "Content-Type": "application/json"})
resp = json.load(urllib.request.urlopen(req, timeout=30))
out = {"reply": resp["choices"][0]["message"]["content"]}
def blocked(fn):
    try:
        fn(); return False
    except Exception:
        return True
tcp = lambda: socket.create_connection(("1.1.1.1", 443), timeout=3)
web = lambda: urllib.request.urlopen("https://example.com/", timeout=10)
out["direct_internet_blocked"] = blocked(tcp)
out["unlisted_host_blocked"] = blocked(web)
out["keys_db_hidden"] = blocked(lambda: open(sys.argv[1], "rb").read(1))
open("written.txt", "w").write("x")
print("RESULT " + json.dumps(out))
"""


def test_isolated_serve_and_run(tmp_path: Path) -> None:
    env = {**os.environ}
    paths = Paths(tmp_path / "cfg", tmp_path / "data", tmp_path / "state")
    env.update(
        CAIRNWARD_CONFIG_DIR=str(paths.config_dir),
        CAIRNWARD_DATA_DIR=str(paths.data_dir),
        CAIRNWARD_STATE_DIR=str(paths.state_dir),
    )
    if locate(paths) is None:
        pytest.skip("sandbox-runtime not installed (cairnward sandbox install / CAIRNWARD_SRT)")
    ollama_port, gw, dash, eg = free_port(), free_port(), free_port(), free_port()
    srv = ThreadingHTTPServer(("127.0.0.1", ollama_port), FakeOllama)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    (paths.config_dir / "environments").mkdir(parents=True)
    (paths.config_dir / "cairnward.toml").write_text(
        f"[server]\ngateway_port = {gw}\ndashboard_port = {dash}\negress_port = {eg}\n"
        '[privacy]\npreset = "strict"\n'
    )
    (paths.config_dir / "backends.toml").write_text(
        f'[backends.local-ollama]\ndriver = "ollama"\nurl = "http://127.0.0.1:{ollama_port}"\n'
        '[roles.main]\nbackend = "local-ollama"\nmodel = "m"\n'
        '[roles.fast]\nbackend = "local-ollama"\nmodel = "m"\n'
    )
    ws = tmp_path / "ws"
    ws.mkdir()
    (tmp_path / "agent.py").write_text(AGENT)
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
        st = subprocess.run(
            [*cw, "status", "--wait", "60"], env=env, capture_output=True, text=True, check=False
        )
        assert "core isolated in the sandbox" in st.stdout, st.stdout + st.stderr
        (paths.data_dir / "keys.db").exists()
        run = subprocess.run(
            [
                *cw,
                "run",
                "--env",
                "coding",
                "--",
                sys.executable,
                str(tmp_path / "agent.py"),
                str(paths.data_dir / "keys.db"),
            ],
            env=env,
            cwd=ws,
            capture_output=True,
            text=True,
            timeout=120,
            check=False,
        )
        line = next((x for x in run.stdout.splitlines() if x.startswith("RESULT ")), None)
        assert line, run.stdout + run.stderr
        result = json.loads(line[7:])
        assert result == {
            "reply": "ok from model",
            "direct_internet_blocked": True,
            "unlisted_host_blocked": True,
            "keys_db_hidden": True,
        }
        assert (ws / "written.txt").exists()
    finally:
        serve.terminate()
        try:
            serve.wait(20)
        except subprocess.TimeoutExpired:
            serve.kill()
        srv.shutdown()
    time.sleep(8)  # the core's watchdog notices egress has gone
    leftovers = subprocess.run(["ps", "-eo", "args"], capture_output=True, text=True).stdout
    assert f"--egress-control {paths.state_dir}" not in leftovers
    audit = subprocess.run(
        [*cw, "audit", "log", "--json"], env=env, capture_output=True, text=True, check=True
    ).stdout
    actions = [json.loads(x)["action"] for x in audit.splitlines()]
    assert "run.start" in actions and "model.call" in actions and "run.end" in actions
    verify = subprocess.run([*cw, "audit", "verify"], env=env, capture_output=True, text=True)
    assert verify.returncode == 0, verify.stdout + verify.stderr
