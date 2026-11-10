"""The Rust egress data path, run in place of the Python one (`[egress] implementation =
"rust"`, ADR-0020 stage 2).

`RustEgress` has the interface `serve` uses on `EgressProxy` (`start`, `serve_forever`, `close`,
`port`, `engine`, `sessions`). It runs `cairnward-egress-rs serve` and answers its questions with a
`DecisionService` built on an `EgressProxy` that's never started: policy, approvals, sessions and
audit records stay in Python, in one implementation.

The binary is found through `CAIRNWARD_EGRESS_RS`, then `cairnward-egress-rs` on PATH, then a
build in the repository's `rust/target/`. Until stage 3, a session that should be TLS-inspected
is refused by the Rust path, not tunnelled unread.
"""

from __future__ import annotations

import asyncio
import contextlib
import os
import shutil
from pathlib import Path
from typing import Any

from cairnward.config.loader import Config
from cairnward.egress.decisions import DecisionService
from cairnward.egress.proxy import EgressProxy, Sessions
from cairnward.policy.engine import PolicyEngine


class RustEgressError(Exception):
    pass


def binary() -> Path | None:
    if found := os.environ.get("CAIRNWARD_EGRESS_RS"):
        return Path(found) if Path(found).is_file() else None
    if on_path := shutil.which("cairnward-egress-rs"):
        return Path(on_path)
    repo = Path(__file__).resolve().parents[3] / "rust" / "target"
    for profile in ("release", "debug"):
        exe = repo / profile / "cairnward-egress-rs"
        if exe.is_file():
            return exe
    return None


class RustEgress:
    def __init__(self, config: Config, engine: PolicyEngine, sessions: Sessions, *,
                 decisions_socket: Path, host: str = "127.0.0.1",
                 port: int | None = None) -> None:  # fmt: skip
        self.decider = EgressProxy(config, engine, sessions, host=host, port=port)
        self.config, self.engine, self.sessions = config, engine, sessions
        self.host, self.port = host, self.decider.port
        self.socket = decisions_socket
        self._service = DecisionService(self.decider)
        self._proc: asyncio.subprocess.Process | None = None

    async def start(self) -> int:
        exe = binary()
        if exe is None:
            raise RustEgressError(
                '[egress] implementation = "rust" but cairnward-egress-rs wasn\'t found: build it '
                "(cargo build --release --manifest-path rust/Cargo.toml) or set CAIRNWARD_EGRESS_RS"
            )
        await self._service.start(self.socket)
        limits = self.config.main.egress
        self._proc = await asyncio.create_subprocess_exec(
            str(exe), "serve", "--listen", f"{self.host}:{self.port}",
            "--decisions", str(self.socket),
            "--connect-timeout", str(limits.connect_timeout_s),
            "--idle-timeout", str(limits.idle_timeout_s),
            "--max-connections", str(limits.max_connections),
            stdin=asyncio.subprocess.DEVNULL, stdout=asyncio.subprocess.PIPE,
            env={"PATH": os.environ.get("PATH", "")},  # nothing secret, nothing it needs
        )  # fmt: skip
        if self._proc.stdout is None:  # can't happen: stdout=PIPE
            raise RustEgressError("no output from cairnward-egress-rs")
        line = (await asyncio.wait_for(self._proc.stdout.readline(), 15)).decode()
        if not line.startswith("listening "):
            await self.close()
            raise RustEgressError(f"cairnward-egress-rs didn't start: {line.strip()!r}")
        self.port = int(line.split()[1])
        return self.port

    async def serve_forever(self) -> None:
        if self._proc is None:
            await self.start()
        if self._proc is None:  # pragma: no cover - start() sets it or raises
            raise RustEgressError("cairnward-egress-rs isn't running")
        code = await self._proc.wait()
        raise RustEgressError(f"cairnward-egress-rs exited ({code})")

    async def close(self) -> None:
        if self._proc is not None and self._proc.returncode is None:
            self._proc.terminate()
            with contextlib.suppress(TimeoutError):
                await asyncio.wait_for(self._proc.wait(), 5)
            if self._proc.returncode is None:
                self._proc.kill()
        await self._service.close()
        with contextlib.suppress(FileNotFoundError):
            self.socket.unlink()


def make_egress(cfg: Config, engine: PolicyEngine, sessions: Sessions, *, socket_dir: Path,
                port: int | None = None) -> Any:  # fmt: skip
    """The data path `[egress] implementation` names: EgressProxy, or RustEgress."""
    if cfg.main.egress.implementation == "rust":
        return RustEgress(cfg, engine, sessions, decisions_socket=socket_dir / "egress-rs.sock",
                          port=port)  # fmt: skip
    return EgressProxy(cfg, engine, sessions, port=port)
