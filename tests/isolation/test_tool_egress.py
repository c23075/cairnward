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
