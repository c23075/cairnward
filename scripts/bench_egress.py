#!/usr/bin/env python3
"""Egress overhead, Python data path against Rust (ADR-0020, stage 5).

Both data paths run against the same local origin with the same policy engine, audit log and
decision logic; a request sent straight to the origin is the baseline. Measured per request:
plain HTTP through the proxy, a CONNECT tunnel (open, echo, close) and, with TLS inspection on,
an HTTPS request. Then throughput with several clients at once. Every allowed connection writes
two audit records that wait for the disk, so use --dir on the data disk, as bench_gateway.py.

    uv run python scripts/bench_egress.py --dir ~/cairnward-bench --requests 300
"""

from __future__ import annotations

import argparse
import asyncio
import shutil
import socket
import ssl
import statistics
import sys
import tempfile
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any

import httpx

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from tests.test_tls_inspection import _origin_cert

from cairnward.audit import AuditLog
from cairnward.config import load_config
from cairnward.config.paths import Paths
from cairnward.egress import EgressProxy, Session, Sessions
from cairnward.egress import tls as cwtls
from cairnward.egress.rust import RustEgress, binary
from cairnward.policy import PolicyEngine


class Server(ThreadingHTTPServer):
    request_queue_size = 256  # the default backlog of 5 drops connections under load


class Origin(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"

    def log_message(self, *a: Any) -> None:
        pass

    def do_GET(self) -> None:
        body = b"x" * 512
        self.send_response(200)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)


def _serve(srv: ThreadingHTTPServer) -> int:
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    return srv.server_address[1]


def _echo() -> int:
    s = socket.create_server(("127.0.0.1", 0), backlog=256)

    def run() -> None:
        while True:
            try:
                c, _ = s.accept()
            except OSError:
                return
            threading.Thread(target=lambda c=c: (c.sendall(c.recv(64)), c.close()),
                             daemon=True).start()  # fmt: skip

    threading.Thread(target=run, daemon=True).start()
    return int(s.getsockname()[1])


def _check(ok: bool) -> None:
    if not ok:
        raise RuntimeError("unexpected answer through the proxy")


def _ms(samples: list[float]) -> tuple[float, float]:
    samples = sorted(samples)
    return statistics.median(samples) * 1000, samples[int(len(samples) * 0.95) - 1] * 1000


def bench(impl: str, folder: Path, n: int, clients: int) -> dict[str, Any]:
    http_port = _serve(Server(("127.0.0.1", 0), Origin))
    echo_port = _echo()
    ca, server_pem = _origin_cert(folder)
    https = Server(("127.0.0.1", 0), Origin)
    ctx = ssl.create_default_context(ssl.Purpose.CLIENT_AUTH)
    ctx.load_cert_chain(server_pem)
    https.socket = ctx.wrap_socket(https.socket, server_side=True)
    tls_port = _serve(https)
    paths = Paths(folder / "cfg", folder / "data", folder / "state")
    (paths.config_dir / "environments").mkdir(parents=True, exist_ok=True)
    paths.state_dir.mkdir(parents=True, exist_ok=True)
    hosts = ", ".join(f'"127.0.0.1:{p}"' for p in (http_port, echo_port, tls_port))
    (paths.config_dir / "environments" / "e.toml").write_text(
        f'[privacy]\nnetwork = "allowlist"\nnetwork_allow_hosts = [{hosts}]\n'
        f'[tls_inspection]\nenabled = true\nunmatched = "allow"\n'
        f'skip_hosts = ["127.0.0.1:{echo_port}"]\n')  # fmt: skip
    cfg = load_config(paths)
    log = AuditLog.open(paths)
    sessions = Sessions()
    engine = PolicyEngine(cfg, audit=log)
    token = sessions.create(Session(environment="e", client="bench", inspect=True))
    upstream = cwtls.upstream_context()
    upstream.load_verify_locations(ca)
    loop = asyncio.new_event_loop()
    threading.Thread(target=loop.run_forever, daemon=True).start()

    def run(coro: Any) -> Any:
        return asyncio.run_coroutine_threadsafe(coro, loop).result(30)

    if impl == "python":
        proxy: Any = EgressProxy(cfg, engine, sessions, port=0, upstream_ssl=upstream)
        port = run(proxy.start())
        asyncio.run_coroutine_threadsafe(proxy.serve_forever(), loop)
    else:
        sock = Path(tempfile.mkdtemp(prefix="cw-b-", dir="/tmp")) / "d.sock"
        proxy = RustEgress(cfg, engine, sessions, decisions_socket=sock, port=0)
        proxy.decider._upstream_ssl = upstream  # unused by Rust; kept for symmetry
        port = run(_rust_start(proxy, ca))
    url = f"http://cw:{token}@127.0.0.1:{port}"
    bundle = cwtls.trust_bundle(paths.data_dir, folder / "bundle.pem")

    failures: dict[str, list[str]] = {}

    def timed(fn: Any) -> list[float]:
        out = []
        for _ in range(n):
            t = time.perf_counter()
            try:
                fn()
            except Exception as exc:  # counted, not fatal: a benchmark reports what happened
                failures.setdefault(fn.__name__, []).append(f"{type(exc).__name__}: {exc}"[:120])
                continue
            out.append(time.perf_counter() - t)
        return out

    def direct() -> None:
        httpx.get(f"http://127.0.0.1:{http_port}/", trust_env=False)

    def plain() -> None:
        with httpx.Client(proxy=url, trust_env=False, timeout=60) as c:
            _check(c.get(f"http://127.0.0.1:{http_port}/").status_code == 200)

    def tunnel() -> None:
        with socket.create_connection(("127.0.0.1", port)) as s:
            import base64

            auth = base64.b64encode(f"cw:{token}".encode()).decode()
            s.sendall(f"CONNECT 127.0.0.1:{echo_port} HTTP/1.1\r\n"
                      f"Proxy-Authorization: Basic {auth}\r\n\r\n".encode())  # fmt: skip
            _check(s.recv(256).startswith(b"HTTP/1.1 200"))
            s.sendall(b"ping")
            _check(s.recv(16) == b"ping")

    def inspected() -> None:
        with httpx.Client(proxy=url, verify=str(bundle), trust_env=False) as c:
            _check(c.get(f"https://127.0.0.1:{tls_port}/").status_code == 200)

    for fn in (plain, tunnel, inspected):
        fn()  # warm up: first certificate, first connections
    result: dict[str, Any] = {"direct": _ms(timed(direct)), "plain": _ms(timed(plain)),
              "tunnel": _ms(timed(tunnel)), "inspected": _ms(timed(inspected))}  # fmt: skip
    print(f"  {impl}: sequential done; {clients} clients at once ...", flush=True)
    t = time.perf_counter()

    def counted(_: int) -> None:
        try:
            plain()
        except Exception as exc:
            failures.setdefault("concurrent", []).append(f"{type(exc).__name__}: {exc}"[:120])

    with ThreadPoolExecutor(clients) as pool:
        list(pool.map(counted, range(n)))
    result["throughput"] = n / (time.perf_counter() - t)
    result["failures"] = {k: (len(v), v[0]) for k, v in failures.items()}
    run(proxy.close())
    log.close()
    return result


async def _rust_start(proxy: RustEgress, ca: Path) -> int:
    # the benchmark's origin has its own CA: give the Rust binary that root, as the tests do
    import os

    os.environ.setdefault("CAIRNWARD_EGRESS_RS", str(binary()))
    real = asyncio.create_subprocess_exec

    async def with_roots(*args: Any, **kw: Any) -> Any:
        return await real(*args, "--upstream-roots", str(ca), **kw)

    asyncio.create_subprocess_exec = with_roots
    try:
        return await proxy.start()
    finally:
        asyncio.create_subprocess_exec = real


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--dir", type=Path, default=Path.cwd() / ".bench-egress",
                    help="where the audit log goes: put it on the data disk")  # fmt: skip
    ap.add_argument("--requests", type=int, default=300)
    ap.add_argument("--clients", type=int, default=8)
    args = ap.parse_args()
    if binary() is None:
        print("build the Rust binary first: cargo build --release --manifest-path rust/Cargo.toml")
        return 1
    print(f"{args.requests} requests each; audit log under {args.dir}; Rust binary {binary()}")
    rows = []
    for impl in ("python", "rust"):
        folder = args.dir / impl
        shutil.rmtree(folder, ignore_errors=True)
        folder.mkdir(parents=True)
        rows.append((impl, bench(impl, folder, args.requests, args.clients)))
    print(f"\n{'':8} {'direct':>14} {'plain HTTP':>14} {'CONNECT':>14} {'inspected':>14} "
          f"{'req/s (' + str(args.clients) + ')':>12}")  # fmt: skip

    def cell(r: dict[str, Any], k: str) -> str:
        return f"{r[k][0]:6.2f}/{r[k][1]:6.2f}"

    for impl, r in rows:
        print(f"{impl:8} {cell(r, 'direct'):>14} {cell(r, 'plain'):>14} {cell(r, 'tunnel'):>14} "
              f"{cell(r, 'inspected'):>14} {r['throughput']:12.0f}")  # fmt: skip
    print("\nmedian/p95 in ms per request (a new connection each); req/s: plain HTTP, all clients")
    for impl, r in rows:
        for what, (count, first) in r["failures"].items():
            print(f"FAILED {impl} {what}: {count} of {args.requests}, e.g. {first}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
