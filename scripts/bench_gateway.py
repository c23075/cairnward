#!/usr/bin/env python3
"""What the gateway adds to a request, excluding model time (NFR-P2).

Starts a stand-in model server that answers at once and a real gateway in front of it, both
on real local sockets, and compares the time of the same request sent directly and through
the gateway. Both run in this process, so the gateway's numbers include some contention
with the stand-in: they lean high, not low. Sends only generated text; talks only to itself.

    uv run python scripts/bench_gateway.py [--requests 200] [--out file.json]
"""

from __future__ import annotations

import argparse
import asyncio
import json
import statistics
import sys
import tempfile
import threading
import time
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any

import httpx
import uvicorn

from cairnward.audit import AuditLog, enabled_for
from cairnward.backends import Backends
from cairnward.config import load_config
from cairnward.config.paths import Paths
from cairnward.gateway import ClientKeys, GatewayDeps, create_app
from cairnward.policy import PolicyEngine
from cairnward.store import Vault

# Target from requirements.md for each mode, in milliseconds.
MODES: dict[str, tuple[str, float]] = {
    "strict": ('preset = "strict"\n', 50.0),
    "all-off": (
        'preset = "open"\nisolation = "off"\nnetwork = "open"\napprovals = "off"\n'
        'redaction = "off"\nvault = "off"\naudit = "off"\naudit_anchor = "off"\n'
        'crypto_shred = "off"\nbackup_purge = "off"\nmcp_pinning = "off"\n'
        'egress_report = "off"\n',
        10.0,
    ),
}
MODEL = "qwen3:1.7b"
MESSAGES = [
    {"role": "system", "content": "You are a helpful assistant. " * 20},
    {"role": "user", "content": "Generated question for the benchmark. " * 40},
]
BACKENDS = """schema_version = 1
[backends.local-ollama]
driver = "ollama"
url = "{url}"
[roles.main]
backend = "local-ollama"
model = "qwen3:30b"
[roles.fast]
backend = "local-ollama"
model = "qwen3:1.7b"
[roles.embed]
backend = "local-ollama"
model = "qwen3-embedding:0.6b"
"""


class StandIn(BaseHTTPRequestHandler):
    """Ollama's reply format, answered at once. HTTP/1.1 so connections are reused, as the
    gateway's client and this script's both do."""

    protocol_version = "HTTP/1.1"
    # Headers and body in one write: as two, TCP's delayed ACK adds 40 ms to every reply,
    # which would be charged to the gateway.
    wbufsize = 65536
    disable_nagle_algorithm = True

    def _send(self, body: dict[str, Any]) -> None:
        data = json.dumps(body).encode()
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def do_GET(self) -> None:
        self._send({"models": []})

    def do_POST(self) -> None:
        self.rfile.read(int(self.headers.get("Content-Length", 0)))
        message = {"role": "assistant", "content": "Generated."}
        self._send({"model": MODEL, "done": True, "done_reason": "stop", "prompt_eval_count": 12,
                    "eval_count": 5, "message": message})  # fmt: skip

    def log_message(self, format: str, *args: Any) -> None:
        return


@contextmanager
def stand_in() -> Iterator[str]:
    httpd = ThreadingHTTPServer(("127.0.0.1", 0), StandIn)
    threading.Thread(target=httpd.serve_forever, daemon=True).start()
    try:
        yield f"http://127.0.0.1:{httpd.server_address[1]}"
    finally:
        httpd.shutdown()
        httpd.server_close()


@contextmanager
def gateway(url: str, privacy: str, folder: Path) -> Iterator[tuple[str, str]]:
    """A real gateway on a real port; yields its URL and a client key."""
    paths = Paths(folder / "cfg", folder / "data", folder / "state")
    paths.config_dir.mkdir(parents=True)
    (paths.config_dir / "backends.toml").write_text(BACKENDS.format(url=url))
    (paths.config_dir / "cairnward.toml").write_text(f"schema_version = 1\n[privacy]\n{privacy}")
    cfg = load_config(paths)
    audit, vault = AuditLog.open(paths, enabled_for(cfg)), Vault.open(paths)  # as the server does
    keys = ClientKeys.at(paths.state_dir)
    key = keys.add("bench")
    backends = Backends(cfg.backends)
    app = create_app(GatewayDeps(cfg, PolicyEngine(cfg, audit=audit), backends, keys, vault))
    server = uvicorn.Server(uvicorn.Config(app, host="127.0.0.1", port=0, log_level="error"))

    async def serve() -> None:  # the clients must close in the loop that used them
        await server.serve()
        await backends.aclose()

    thread = threading.Thread(target=lambda: asyncio.run(serve()), daemon=True)
    thread.start()
    try:
        while not server.started:
            time.sleep(0.01)
        port = server.servers[0].sockets[0].getsockname()[1]
        yield f"http://127.0.0.1:{port}", key
    finally:
        server.should_exit = True
        thread.join(10)
        vault.close()
        audit.close()


def timed(send: Callable[[], httpx.Response], requests: int) -> list[float]:
    """Milliseconds per request, after a few that warm up connections and caches."""
    for _ in range(5):
        send().raise_for_status()
    out = []
    for _ in range(requests):
        started = time.perf_counter()
        resp = send()
        out.append((time.perf_counter() - started) * 1000)
        resp.raise_for_status()
    return out


def measure(mode: str, requests: int = 200, folder: Path | None = None) -> dict[str, Any]:
    privacy, target = MODES[mode]
    with stand_in() as url, tempfile.TemporaryDirectory(prefix="cw-bench-", dir=folder) as tmp:
        direct_body = {"model": MODEL, "messages": MESSAGES, "stream": False}
        with httpx.Client(base_url=url, trust_env=False) as c:
            direct = timed(lambda: c.post("/api/chat", json=direct_body), requests)
        with gateway(url, privacy, Path(tmp)) as (gw, key):
            body = {"model": "cw/coding:low", "messages": MESSAGES}
            h = {"Authorization": f"Bearer {key}"}
            with httpx.Client(base_url=gw, headers=h, trust_env=False, timeout=30) as c:
                through = timed(lambda: c.post("/v1/chat/completions", json=body), requests)
    base = statistics.median(direct)
    return {
        "mode": mode,
        "requests": requests,
        "target_ms": target,
        "direct_median_ms": round(base, 2),
        "overhead_median_ms": round(statistics.median(through) - base, 2),
        "overhead_p95_ms": round(statistics.quantiles(through, n=20)[18] - base, 2),
    }


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--requests", type=int, default=200)
    ap.add_argument("--dir", type=Path, help="where to keep the test data: use the disk your "
                    "data folder is on (audit and vault writes wait for the disk)")  # fmt: skip
    ap.add_argument("--out", type=Path)
    args = ap.parse_args(argv)
    results = [measure(mode, args.requests, args.dir) for mode in MODES]
    for r in results:
        print(f"{r['mode']:8} overhead median {r['overhead_median_ms']:7.2f} ms, "
              f"p95 {r['overhead_p95_ms']:7.2f} ms (target {r['target_ms']:g} ms)")  # fmt: skip
    if args.out:
        args.out.write_text(json.dumps(results, indent=2), encoding="utf-8")
    return 0 if all(r["overhead_median_ms"] <= r["target_ms"] for r in results) else 1


if __name__ == "__main__":
    sys.exit(main())
