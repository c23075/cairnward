"""Time from `cairnward serve` starting until the gateway's /health answers (NFR-P5, I15).

    uv run python scripts/bench_startup.py [--runs 8] [--existing] [--off]

Each run uses its own config, data and state folders under the system temp folder (short paths:
Unix sockets allow 107 bytes), with the Strict preset and a backend that isn't there, so nothing
but Cairnward itself is timed. The first run only warms the file cache and isn't counted.

--existing  reuse the first run's data for the counted runs, as a server that has run before
            does (its databases exist, so no migrations run). Without it, every run starts on
            empty data and creates its databases.
--off       isolation off (one process), for comparison. The default is isolated, which needs
            the sandbox (`cairnward sandbox install`).
"""

from __future__ import annotations

import argparse
import os
import shutil
import socket
import statistics
import subprocess
import sys
import tempfile
import time
import urllib.request
from pathlib import Path


def free_port() -> int:
    s = socket.socket()
    s.bind(("127.0.0.1", 0))
    port: int = s.getsockname()[1]
    s.close()
    return port


def once(root: Path, *, isolated: bool, timeout: float = 60) -> float:
    cfg = root / "c"
    (cfg / "environments").mkdir(parents=True, exist_ok=True)
    gw, dash, eg = free_port(), free_port(), free_port()
    off = "" if isolated else 'isolation = "off"\n'
    (cfg / "cairnward.toml").write_text(
        f"[server]\ngateway_port = {gw}\ndashboard_port = {dash}\negress_port = {eg}\n"
        f'[privacy]\npreset = "strict"\n{off}'
    )
    (cfg / "backends.toml").write_text(
        '[backends.local-ollama]\ndriver = "ollama"\nurl = "http://127.0.0.1:9"\n'
        '[roles.main]\nbackend = "local-ollama"\nmodel = "m"\n'
        '[roles.fast]\nbackend = "local-ollama"\nmodel = "m"\n'
    )
    env = {
        **{k: v for k, v in os.environ.items() if k.lower() not in ("http_proxy", "https_proxy")},
        "CAIRNWARD_CONFIG_DIR": str(cfg),
        "CAIRNWARD_DATA_DIR": str(root / "d"),
        "CAIRNWARD_STATE_DIR": str(root / "s"),
        "CAIRNWARD_CACHE_DIR": str(root / "k"),
    }
    log = (root / "serve.log").open("w")
    started = time.monotonic()
    proc = subprocess.Popen(
        [sys.executable, "-m", "cairnward", "serve"],
        env=env,
        stdin=subprocess.DEVNULL,
        stdout=log,
        stderr=subprocess.STDOUT,
    )
    try:
        while time.monotonic() - started < timeout:
            try:
                with urllib.request.urlopen(f"http://127.0.0.1:{gw}/health", timeout=1) as r:
                    if r.status == 200:
                        return time.monotonic() - started
            except OSError:
                time.sleep(0.02)
        raise SystemExit(f"no /health within {timeout:.0f} s; see {root / 'serve.log'}")
    finally:
        proc.terminate()
        try:
            proc.wait(20)
        except subprocess.TimeoutExpired:
            proc.kill()
        log.close()


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument("--runs", type=int, default=8)
    ap.add_argument("--existing", action="store_true", help="reuse one data folder")
    ap.add_argument("--off", action="store_true", help="isolation off")
    args = ap.parse_args()
    isolated = not args.off
    roots: list[Path] = []

    def new_root() -> Path:
        roots.append(Path(tempfile.mkdtemp(prefix="cwst")))
        return roots[-1]

    try:
        first = new_root()
        once(first, isolated=isolated)
        times = [once(first if args.existing else new_root(), isolated=isolated)
                 for _ in range(args.runs)]  # fmt: skip
    finally:
        for r in roots:
            shutil.rmtree(r, ignore_errors=True)
    kind = "existing data" if args.existing else "empty data"
    mode = "isolated" if isolated else "isolation off"
    print(f"{mode}, {kind}: " + " ".join(f"{t:.2f}" for t in times))
    print(f"median {statistics.median(times):.2f} s, range {min(times):.2f} to {max(times):.2f} s")
    return 0


if __name__ == "__main__":
    sys.exit(main())
