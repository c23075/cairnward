"""End to end with the real sandbox: `serve` in each mode + `cairnward run` (Linux, srt installed).

Skipped unless sandbox-runtime is available (CI's isolation job installs it).
"""

import json
import subprocess
import sys
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any

import pytest

from cairnward.sandbox import locate
from tests.isolation.harness import MODES, Cairnward, free_port

pytestmark = pytest.mark.skipif(not sys.platform.startswith("linux"), reason="Linux only")


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


@pytest.mark.parametrize("mode", MODES)
def test_serve_and_run(tmp_path: Path, mode: str) -> None:
    srv = ThreadingHTTPServer(("127.0.0.1", 0), FakeOllama)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    cw = Cairnward(
        tmp_path,
        mode,
        backend_url=f"http://127.0.0.1:{srv.server_address[1]}",
        models=("m", "m", "e"),
    )
    paths = cw.paths
    ws = tmp_path / "ws"
    ws.mkdir()
    (tmp_path / "agent.py").write_text(AGENT)
    try:
        with cw:
            # the dashboard (in isolated mode served by the core through a forwarder)
            import httpx

            link = cw.cw("dashboard", "--no-open", check=True).stdout.splitlines()[1]
            with httpx.Client(base_url=f"http://127.0.0.1:{cw.dash}", trust_env=False) as web:
                assert web.get(link.split(str(cw.dash), 1)[1]).status_code == 303
                info = web.get("/api/session").json()
                assert info["isolated"] is cw.core_isolated
                custom = "" if mode == "isolated" else " (custom: isolation)"
                assert info["privacy"] == "Strict" + custom, info["privacy"]
            csock = paths.state_dir / "clients" / "gateway.sock"
            with httpx.Client(transport=httpx.HTTPTransport(uds=str(csock)), timeout=10) as uc:
                assert uc.get(f"http://127.0.0.1:{cw.gw}/health").json()["status"] == "ok"
            run = cw.cw("run", "--env", "coding", "--", sys.executable,
                        str(tmp_path / "agent.py"), str(paths.data_dir / "keys.db"),
                        cwd=ws)  # fmt: skip
            line = next((x for x in run.stdout.splitlines() if x.startswith("RESULT ")), None)
            assert line, run.stdout + run.stderr
            result = json.loads(line[7:])
            assert result["reply"] == "ok from model"
            if cw.coding_sandboxed:
                assert result == {
                    "reply": "ok from model",
                    "direct_internet_blocked": True,
                    "unlisted_host_blocked": True,
                    "keys_db_hidden": True,
                }
            else:  # isolation off: the agent isn't sandboxed (as the setting says)
                assert result["keys_db_hidden"] is False
                assert "isolation" in run.stderr.lower() or "not sandboxed" in run.stderr.lower(), (
                    run.stderr
                )
            assert (ws / "written.txt").exists()
            # the core's background work runs in every mode: the job runner picks up a
            # review of an empty library, which fails at its first step
            (tmp_path / "papers").mkdir()
            created = cw.cw("library", "create", "empty", str(tmp_path / "papers"))
            assert created.returncode == 0, created.stdout + created.stderr
            queued = cw.cw("research", "review", "empty", "--question", "Anything?")
            assert queued.returncode == 0, queued.stdout + queued.stderr
            shown = cw.wait_job(queued.stdout.split()[1], timeout=90)
            assert " failed " in shown and "no indexed papers" in shown, shown
    finally:
        srv.shutdown()
    if cw.core_isolated:
        time.sleep(8)  # the core's watchdog notices egress has gone
        leftovers = subprocess.run(["ps", "-eo", "args"], capture_output=True, text=True).stdout
        assert f"--egress-control {paths.state_dir}" not in leftovers
    actions = [r["action"] for r in cw.audit()]
    assert "run.start" in actions and "model.call" in actions and "run.end" in actions
    verify = cw.cw("audit", "verify")
    assert verify.returncode == 0, verify.stdout + verify.stderr


ASK_AGENT = r"""
import json, sys, urllib.request, urllib.error
out = []
for url in sys.argv[1:]:
    try:
        out.append(urllib.request.urlopen(url, timeout=60).read().decode())
    except urllib.error.HTTPError as e:
        out.append(f"HTTP {e.code}: {e.read().decode(errors='replace')[:300]}")
    except Exception as e:
        out.append(f"error: {e}")
print("RESULT " + json.dumps(out))
"""


class Origin(BaseHTTPRequestHandler):
    def log_message(self, *a: Any) -> None:
        pass

    def do_GET(self) -> None:
        body = b"approved origin"
        self.send_response(200)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)


@pytest.mark.parametrize("mode", ["isolated", "hybrid"])  # needs a sandbox to ask
def test_run_asks_for_new_hosts(tmp_path: Path, mode: str) -> None:
    """network = ask: the sandbox asks, `cairnward approvals answer` says yes, the agent
    connects; a local address that isn't listed is refused unasked."""
    srv = ThreadingHTTPServer(("127.0.0.1", 0), FakeOllama)
    org = ThreadingHTTPServer(("127.0.0.1", 0), Origin)
    for s in (srv, org):
        threading.Thread(target=s.serve_forever, daemon=True).start()
    org_port = org.server_address[1]
    # The origin is reached by name ("localhost"): not on the sandbox's list, so it asks;
    # egress still refuses loopback addresses unless that exact address is listed.
    cw = Cairnward(tmp_path, mode, backend_url=f"http://127.0.0.1:{srv.server_address[1]}",
                   privacy='preset = "strict"\nnetwork = "ask"\n'
                           f'network_allow_hosts = ["127.0.0.1:{org_port}"]\n')  # fmt: skip
    srt = locate(cw.paths)
    if srt is None or not srt[-1].endswith("cli.js"):
        pytest.skip("needs sandbox-runtime as the node package (cairnward sandbox install)")
    env = cw.env
    ws = tmp_path / "ws"
    ws.mkdir()
    (tmp_path / "agent.py").write_text(ASK_AGENT)
    cw_cmd = [sys.executable, "-m", "cairnward"]
    try:
        cw.__enter__()
        run = subprocess.Popen(
            [*cw_cmd, "run", "--env", "coding", "--", sys.executable, str(tmp_path / "agent.py"),
             f"http://localhost:{org_port}/", f"http://127.1:{free_port()}/"],
            env=env, cwd=ws, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True,
        )  # fmt: skip
        pending: list[dict[str, Any]] = []
        deadline = time.monotonic() + 60
        while not pending and time.monotonic() < deadline:
            out = subprocess.run([*cw_cmd, "approvals", "list", "--json"], env=env,
                                 capture_output=True, text=True, check=True).stdout  # fmt: skip
            pending = json.loads(out)
            time.sleep(0.2)
        assert pending, "the sandbox never asked"
        (item,) = pending
        assert item["target"] == f"localhost:{org_port}" and item["run_id"]
        subprocess.run([*cw_cmd, "approvals", "answer", item["id"], "y"], env=env, check=True)
        stdout, stderr = run.communicate(timeout=90)
        line = next((x for x in stdout.splitlines() if x.startswith("RESULT ")), None)
        assert line, stdout + stderr
        approved, local = json.loads(line[7:])
        assert approved == "approved origin"
        # another local port: refused without asking (the sandbox reaches this machine
        # directly, so Cairnward applies egress's address guard when the sandbox asks)
        assert local.startswith("HTTP 403"), local
    finally:
        cw.__exit__()
        srv.shutdown()
        org.shutdown()
    recs = cw.audit(100)
    assert any(r["action"] == "approval" and r["target"] == f"localhost:{org_port}"
               for r in recs)  # fmt: skip
    assert any(r["action"] == "net.connect" and r["decision"] == "deny"
               and r["target"].startswith("127.0.0.1:") for r in recs)  # fmt: skip
