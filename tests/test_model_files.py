"""Verified model downloads through egress (FR-SUP-2)."""

import asyncio
import hashlib
import json
import struct
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any

import httpx
import pytest

from cairnward import model_files as mf
from cairnward.audit import AuditLog
from cairnward.config import load_config
from cairnward.config.paths import Paths
from cairnward.core.management import management_app
from cairnward.egress import EgressProxy, Sessions
from cairnward.egress.control import LocalEgress
from cairnward.gateway.clients import RunKeys
from cairnward.policy import PolicyEngine

GGUF = b"GGUF" + b"\x03\x00\x00\x00" + b"\x00" * 1000
header = json.dumps({"w": {"dtype": "F32", "shape": [1], "data_offsets": [0, 4]}}).encode()
SAFE = struct.pack("<Q", len(header)) + header + b"\x00" * 4


def sha(b: bytes) -> str:
    return hashlib.sha256(b).hexdigest()


def serve(files: dict[str, bytes]) -> httpx.MockTransport:
    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/redirect/m.gguf":
            return httpx.Response(302, headers={"Location": "https://cdn.example/m.gguf"})
        body = files.get(request.url.path.lstrip("/").split("/")[-1])
        return httpx.Response(200, content=body) if body is not None else httpx.Response(404)

    return httpx.MockTransport(handler)


def test_names() -> None:
    assert (
        mf.file_name("https://h/x/Qwen3-30B-Q4_K_M.gguf?download=1", None)
        == "Qwen3-30B-Q4_K_M.gguf"
    )
    for bad in ("https://h/model.bin", "https://h/model.pt", "https://h/model.pkl", "https://h/"):
        with pytest.raises(mf.DownloadError):
            mf.file_name(bad, None)
    with pytest.raises(mf.DownloadError):
        mf.file_name("https://h/x", "../evil.gguf")


def test_download_checks(tmp_path: Path) -> None:
    t = serve({"m.gguf": GGUF, "w.safetensors": SAFE, "fake.gguf": b"PK\x03\x04" + b"x" * 50})
    got = mf.download("https://hf.example/redirect/m.gguf", sha(GGUF), tmp_path, transport=t)
    assert got.path.read_bytes() == GGUF and got.size == len(GGUF)
    assert mf.listed(tmp_path)["m.gguf"]["sha256"] == sha(GGUF)
    with pytest.raises(mf.DownloadError, match="already exists"):
        mf.download("https://hf.example/m.gguf", sha(GGUF), tmp_path, transport=t)
    assert mf.download("https://h/w.safetensors", sha(SAFE), tmp_path, transport=t).size == len(
        SAFE
    )
    with pytest.raises(mf.DownloadError, match="SHA-256 mismatch"):
        mf.download("https://h/m.gguf", "0" * 64, tmp_path, name="other.gguf", transport=t)
    with pytest.raises(mf.DownloadError, match="bad magic"):
        mf.download("https://h/fake.gguf", sha(b"PK\x03\x04" + b"x" * 50), tmp_path, transport=t)
    with pytest.raises(mf.DownloadError, match="HTTP 404"):
        mf.download("https://h/missing.gguf", sha(GGUF), tmp_path, transport=t)
    with pytest.raises(mf.DownloadError, match="64 hexadecimal"):
        mf.download("https://h/m.gguf", "abc", tmp_path, transport=t)
    assert not list(tmp_path.glob(".*.part")) and not (tmp_path / "other.gguf").exists()


class Origin(BaseHTTPRequestHandler):
    def log_message(self, *a: Any) -> None:
        pass

    def do_GET(self) -> None:
        self.send_response(200)
        self.send_header("Content-Length", str(len(GGUF)))
        self.end_headers()
        self.wfile.write(GGUF)


async def test_through_egress(tmp_path: Path) -> None:
    paths = Paths(tmp_path / "cfg", tmp_path / "data", tmp_path / "state")
    origin = ThreadingHTTPServer(("127.0.0.1", 0), Origin)
    threading.Thread(target=origin.serve_forever, daemon=True).start()
    port = origin.server_address[1]
    (paths.config_dir / "environments").mkdir(parents=True)
    (paths.config_dir / "environments" / "dl.toml").write_text(
        f'[privacy]\nnetwork = "allowlist"\nnetwork_allow_hosts = ["127.0.0.1:{port}"]\n'
    )
    cfg = load_config(paths)
    sessions = Sessions()
    with AuditLog.open(paths) as audit:
        proxy = EgressProxy(cfg, PolicyEngine(cfg, audit=audit), sessions, port=0)
        await proxy.start()
        task = asyncio.create_task(proxy.serve_forever())
        app = management_app(cfg, audit=audit, run_keys=RunKeys(), egress=LocalEgress(sessions),
                             isolated=False)  # fmt: skip
        try:
            async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app),
                                         base_url="http://cairnward") as c:  # fmt: skip
                sess = (await c.post("/v1/egress/session", json={"environment": "dl"})).json()
                url = f"http://cw:{sess['token']}@127.0.0.1:{proxy.port}"
                got = await asyncio.to_thread(
                    mf.download, f"http://127.0.0.1:{port}/m.gguf", sha(GGUF), tmp_path / "m",
                    proxy=url,
                )  # fmt: skip
                assert got.size == len(GGUF)
                # a host the policy doesn't allow is refused by egress
                with pytest.raises(mf.DownloadError, match="403"):
                    await asyncio.to_thread(mf.download, "http://10.9.9.9/x.gguf", sha(GGUF),
                                            tmp_path / "m", proxy=url)  # fmt: skip
                await c.post("/v1/egress/session/end", json={"session": sess["session"]})
                assert sessions._by_token == {}
        finally:
            task.cancel()
            await proxy.close()
            origin.shutdown()
        recs = [(r.action, r.phase, r.decision, r.client) for r in audit.records()]
        assert ("net.connect", "pre", "allow", "cairnward-download") in recs
        assert ("net.connect", "event", "deny", "cairnward-download") in recs
