"""RustEgress (egress/rust.py): choosing the data path, a missing binary, and a real round trip
through cairnward-egress-rs when it's built (ADR-0020, stage 2)."""

import asyncio
import base64
import tempfile
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any

import httpx
import pytest

from cairnward.audit import AuditLog
from cairnward.config import load_config
from cairnward.config.paths import Paths
from cairnward.egress import EgressProxy, Session, Sessions
from cairnward.egress import rust as rust_egress
from cairnward.policy import PolicyEngine

pytestmark = pytest.mark.skipif(not hasattr(asyncio, "start_unix_server"), reason="Unix sockets")


class Origin(BaseHTTPRequestHandler):
    def log_message(self, *a: Any) -> None:
        pass

    def do_GET(self) -> None:
        self.send_response(200)
        self.send_header("Content-Length", "5")
        self.end_headers()
        self.wfile.write(b"hello")


def _setup(tmp_path: Path, main: str, privacy: str) -> tuple[Any, PolicyEngine, Sessions, AuditLog]:
    paths = Paths(tmp_path / "cfg", tmp_path / "data", tmp_path / "state")
    (paths.config_dir / "environments").mkdir(parents=True, exist_ok=True)
    (paths.config_dir / "environments" / "e.toml").write_text(f"[privacy]\n{privacy}\n")
    (paths.config_dir / "cairnward.toml").write_text(main)
    cfg = load_config(paths)
    log = AuditLog.open(paths)
    return cfg, PolicyEngine(cfg, audit=log), Sessions(), log


def test_the_setting_picks_the_data_path(tmp_path: Path) -> None:
    cfg, engine, sessions, log = _setup(tmp_path, "", 'network = "off"')
    assert isinstance(rust_egress.make_egress(cfg, engine, sessions, socket_dir=tmp_path),
                      EgressProxy)  # fmt: skip
    cfg2, *_ = _setup(tmp_path, '[egress]\nimplementation = "rust"\n', 'network = "off"')
    made = rust_egress.make_egress(cfg2, engine, sessions, socket_dir=tmp_path, port=0)
    assert isinstance(made, rust_egress.RustEgress) and made.socket == tmp_path / "egress-rs.sock"
    log.close()


def test_a_missing_binary_is_a_clear_error(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("CAIRNWARD_EGRESS_RS", str(tmp_path / "nowhere"))
    assert rust_egress.binary() is None
    cfg, engine, sessions, log = _setup(tmp_path, '[egress]\nimplementation = "rust"\n', "")
    egress = rust_egress.RustEgress(cfg, engine, sessions, decisions_socket=tmp_path / "d.sock",
                                    port=0)  # fmt: skip
    with pytest.raises(rust_egress.RustEgressError, match="wasn't found"):
        asyncio.run(egress.start())
    log.close()


@pytest.mark.skipif(rust_egress.binary() is None, reason="Rust egress binary not built")
def test_a_request_through_the_rust_data_path(tmp_path: Path) -> None:
    origin = ThreadingHTTPServer(("127.0.0.1", 0), Origin)
    threading.Thread(target=origin.serve_forever, daemon=True).start()
    port = origin.server_address[1]
    cfg, engine, sessions, log = _setup(
        tmp_path, '[egress]\nimplementation = "rust"\n',
        f'network = "allowlist"\nnetwork_allow_hosts = ["127.0.0.1:{port}"]')  # fmt: skip
    token = sessions.create(Session(environment="e", client="t"))
    sock = Path(tempfile.mkdtemp(prefix="cw-rr-", dir="/tmp")) / "d.sock"

    async def main() -> tuple[str, int, int]:
        egress = rust_egress.RustEgress(cfg, engine, sessions, decisions_socket=sock, port=0)
        listen = await egress.start()
        proxy = f"http://cw:{token}@127.0.0.1:{listen}"

        def fetch() -> tuple[str, int, int]:
            with httpx.Client(proxy=proxy, trust_env=False, timeout=10) as c:
                ok = c.get(f"http://127.0.0.1:{port}/").text
                refused = c.get(f"http://127.0.0.1:{port + 1}/").status_code
            bad = base64.b64encode(b"cw:wrong").decode()
            with httpx.Client(trust_env=False, timeout=10) as c:
                unknown = c.get(
                    f"http://127.0.0.1:{listen}/", headers={"Proxy-Authorization": f"Basic {bad}"}
                ).status_code
            return ok, refused, unknown

        try:
            return await asyncio.to_thread(fetch)
        finally:
            await egress.close()

    try:
        assert asyncio.run(main()) == ("hello", 403, 407)
    finally:
        origin.shutdown()
    assert not sock.exists()
    actions = [(r.phase, r.decision) for r in log.records() if r.action == "net.connect"]
    assert ("pre", "allow") in actions and ("event", "deny") in actions
    post = next(r for r in log.records() if r.phase == "post")
    assert post.details["status"] == 200 and post.details["bytes_down"] == 5
    log.close()
