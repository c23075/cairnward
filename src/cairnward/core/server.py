"""Running Cairnward: `cairnward serve` (spec §3, ADR-0010, ADR-0013).

Single-process mode (isolation off, or not available):
    gateway on 127.0.0.1:<gateway_port>, management API on <state>/cairnward.sock,
    cairnward-egress on 127.0.0.1:<egress_port>, all in one process.

Isolated mode (isolation on, Linux, sandbox-runtime installed), two processes:
    A. this process, the only one with real network access: cairnward-egress, its control
       socket, and a byte-for-byte forwarder 127.0.0.1:<gateway_port> -> gateway socket.
    B. the core (gateway, management, vault, policy) inside srt: no network except srt's
       proxy, which allows the local model servers and hands everything else to egress.
"""

from __future__ import annotations

import asyncio
import contextlib
import os
import subprocess
import sys
import tempfile
from collections.abc import Callable
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit

import uvicorn

from cairnward.audit import AuditLog, enabled_for, record_settings
from cairnward.backends import Backends
from cairnward.config.loader import Config
from cairnward.core.management import management_app
from cairnward.egress import EgressProxy, Session, Sessions
from cairnward.egress.control import EgressControl, LocalEgress, RemoteEgress, control_app
from cairnward.gateway import ClientKeys, GatewayDeps, create_app
from cairnward.gateway.clients import RunKeys
from cairnward.policy import Approver, DenyApprover, PolicyEngine, TerminalApprover
from cairnward.sandbox import SandboxSpec, SandboxUnavailable, require, secret_dirs, srt_hosts, wrap
from cairnward.store import Vault

Log = Callable[[str], None]


def sockets(cfg: Config) -> dict[str, Path]:
    """Socket paths, in the state folder (kept at 0700: only the owner can reach them)."""
    s = cfg.paths.state_dir
    return {
        "management": s / "cairnward.sock",
        "gateway": s / "gateway.sock",
        "egress": s / "egress.sock",
    }


def _approver() -> Approver:
    return TerminalApprover() if sys.stdin.isatty() else DenyApprover()


def private_dir(path: Path) -> None:
    path.mkdir(parents=True, exist_ok=True)
    if sys.platform != "win32":
        path.chmod(0o700)


def _uds_server(app: Any, path: Path) -> uvicorn.Server:
    private_dir(path.parent)
    with contextlib.suppress(FileNotFoundError):
        path.unlink()
    return uvicorn.Server(uvicorn.Config(app, uds=str(path), log_level="warning", access_log=False))


async def _serve_uds(server: uvicorn.Server, path: Path) -> None:
    """Serve on a Unix socket usable by the owner only (FR-API-2).

    uvicorn sets 0666 on the socket it creates, so the mode is corrected once it exists;
    the 0700 state folder already keeps other users out in the meantime.
    """
    task = asyncio.create_task(server.serve())
    while not server.started and not task.done():
        await asyncio.sleep(0.02)
    if path.exists():
        path.chmod(0o600)
    await task


def _core_parts(
    cfg: Config,
    audit: AuditLog,
    vault: Vault,
    egress: EgressControl | None,
    *,
    approver: Approver,
    backends: Backends,
    isolated: bool,
) -> tuple[Any, Any]:
    engine = PolicyEngine(cfg, approver=approver, audit=audit)
    run_keys = RunKeys()
    from cairnward.gateway.scheduling import BenchProfile

    gateway = create_app(
        GatewayDeps(
            cfg,
            engine,
            backends,
            ClientKeys.at(cfg.paths.state_dir),
            vault,
            run_keys,
            profile=BenchProfile.at(cfg.paths.state_dir),
        )
    )
    management = management_app(
        cfg, audit=audit, run_keys=run_keys, egress=egress, isolated=isolated
    )
    return gateway, management


# ------------------------------------------------------------------ single process


def serve_single(cfg: Config, *, with_egress: bool, log: Log) -> None:
    server = cfg.main.server
    socks = sockets(cfg)
    with AuditLog.open(cfg.paths, enabled_for(cfg)) as audit, Vault.open(cfg.paths) as vault:
        sync = record_settings(cfg, audit)
        approver = _approver()
        sessions = Sessions()
        egress_ctl = LocalEgress(sessions) if with_egress else None
        core_token = sessions.create(
            Session(environment=cfg.main.defaults.environment, trusted_core=True)
        )
        egress_url = (
            f"http://cw:{core_token}@127.0.0.1:{server.egress_port}" if with_egress else None
        )
        backends = Backends(cfg.backends, egress_url=egress_url)
        gateway, management = _core_parts(
            cfg, audit, vault, egress_ctl, approver=approver, backends=backends, isolated=False
        )
        gw = uvicorn.Server(
            uvicorn.Config(
                gateway,
                host=server.gateway_host,
                port=server.gateway_port,
                log_level="warning",
                access_log=False,
            )
        )
        mgmt = _uds_server(management, socks["management"])

        async def run() -> None:
            tasks = [gw.serve(), _serve_uds(mgmt, socks["management"])]
            if with_egress:
                proxy = EgressProxy(
                    cfg, PolicyEngine(cfg, approver=approver, audit=audit), sessions
                )
                await proxy.start()
                tasks.append(proxy.serve_forever())
                log(f"cairnward-egress on 127.0.0.1:{proxy.port}")
            log(
                f"Cairnward gateway on http://{server.gateway_host}:{server.gateway_port}/v1 "
                f"(privacy: {cfg.privacy().label}; single process, core not isolated)"
            )
            if sync.changes:
                log(f"Recorded {len(sync.changes)} settings change(s) in the audit log.")
            try:
                await asyncio.gather(*tasks)
            finally:
                await backends.aclose()

        asyncio.run(run())


# ------------------------------------------------------------------ isolated: process A


async def _forward(local_port: int, uds: Path) -> asyncio.Server:
    async def handle(reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        try:
            up_r, up_w = await asyncio.open_unix_connection(str(uds))
        except OSError:
            writer.close()
            return

        async def copy(src: asyncio.StreamReader, dst: asyncio.StreamWriter) -> None:
            try:
                while chunk := await src.read(65536):
                    dst.write(chunk)
                    await dst.drain()
            except ConnectionError:
                pass
            finally:
                with contextlib.suppress(Exception):
                    dst.write_eof() if dst.can_write_eof() else dst.close()

        await asyncio.gather(copy(reader, up_w), copy(up_r, writer))
        for w in (writer, up_w):
            with contextlib.suppress(Exception):
                w.close()

    return await asyncio.start_server(handle, "127.0.0.1", local_port)


def core_sandbox(cfg: Config) -> SandboxSpec:
    """What the core may reach: local model servers and backend hosts (handed to egress)."""
    hosts = []
    for b in cfg.backends.backends.values():
        u = urlsplit(b.url)
        port = u.port or (443 if u.scheme == "https" else 80)
        host = u.hostname or ""
        hosts.append(f"[{host}]:{port}" if ":" in host else f"{host}:{port}")
    p = cfg.paths
    return SandboxSpec(
        allowed_hosts=srt_hosts(hosts),
        allow_write=[p.data_dir, p.state_dir],
        deny_read=secret_dirs(),
        allow_all_unix_sockets=True,  # the core serves and calls Unix sockets
    )


def serve_isolated(cfg: Config, *, log: Log) -> int:
    srt = require(cfg.paths)  # raises SandboxUnavailable
    server = cfg.main.server
    socks = sockets(cfg)
    for path in socks.values():
        private_dir(path.parent)
        with contextlib.suppress(FileNotFoundError):
            path.unlink()
    with AuditLog.open(cfg.paths, enabled_for(cfg)) as audit:
        record_settings(cfg, audit)
        sessions = Sessions()
        core_token = sessions.create(
            Session(environment=cfg.main.defaults.environment, trusted_core=True)
        )
        engine = PolicyEngine(cfg, approver=_approver(), audit=audit)
        proxy = EgressProxy(cfg, engine, sessions)
        ctl = _uds_server(control_app(sessions), socks["egress"])

        with tempfile.TemporaryDirectory(prefix="cairnward-core-") as tmp:
            settings = core_sandbox(cfg).write(Path(tmp) / "srt.json")
            env = {
                k: v
                for k, v in os.environ.items()
                if k in {"PATH", "HOME", "LANG", "LC_ALL", "TERM", "USER", "LOGNAME", "TZ"}
                or k.startswith("CAIRNWARD_")
                or k in {"VIRTUAL_ENV", "PYTHONPATH"}
            }
            egress_url = f"http://cw:{core_token}@127.0.0.1:{server.egress_port}"
            env.update(HTTP_PROXY=egress_url, HTTPS_PROXY=egress_url)  # srt's parent proxy
            cmd = wrap(
                srt,
                settings,
                [
                    sys.executable,
                    "-m",
                    "cairnward",
                    "_core",
                    "--egress-control",
                    str(socks["egress"]),
                ],
            )

            async def run() -> int:
                import signal

                main = asyncio.current_task()
                loop = asyncio.get_running_loop()
                with contextlib.suppress(NotImplementedError):
                    loop.add_signal_handler(signal.SIGTERM, lambda: main and main.cancel())
                await proxy.start()
                ctl_task = asyncio.create_task(_serve_uds(ctl, socks["egress"]))
                fwd = await _forward(server.gateway_port, socks["gateway"])
                core = await asyncio.create_subprocess_exec(*cmd, env=env, stdin=subprocess.DEVNULL)
                log(f"cairnward-egress on 127.0.0.1:{proxy.port} (only networked process)")
                log(
                    f"Cairnward gateway on http://127.0.0.1:{server.gateway_port}/v1 "
                    f"(privacy: {cfg.privacy().label}; core isolated, pid {core.pid})"
                )
                serve_task = asyncio.create_task(proxy.serve_forever())
                try:
                    return await core.wait()
                finally:
                    if core.returncode is None:
                        core.terminate()
                        with contextlib.suppress(Exception):
                            await asyncio.wait_for(core.wait(), 10)
                    fwd.close()
                    ctl.should_exit = True
                    serve_task.cancel()
                    await proxy.close()
                    with contextlib.suppress(asyncio.CancelledError, Exception):
                        await ctl_task

            try:
                return asyncio.run(run())
            except asyncio.CancelledError:
                log("stopped (SIGTERM)")
                return 0


# ------------------------------------------------------------------ isolated: process B


def run_core(cfg: Config, *, egress_control: Path, log: Log) -> None:
    """Inside the sandbox: gateway and management on Unix sockets."""
    socks = sockets(cfg)
    srt_proxy = os.environ.get("HTTP_PROXY", "")
    if srt_proxy and "://" not in srt_proxy:
        srt_proxy = f"http://{srt_proxy}"
    with AuditLog.open(cfg.paths, enabled_for(cfg)) as audit, Vault.open(cfg.paths) as vault:
        # No terminal in here; asks are refused until the dashboard inbox exists (M2).
        backends = Backends(cfg.backends, proxy_all=srt_proxy or None)
        egress = RemoteEgress(str(egress_control))
        gateway, management = _core_parts(
            cfg, audit, vault, egress, approver=DenyApprover(), backends=backends, isolated=True
        )
        gw = _uds_server(gateway, socks["gateway"])
        mgmt = _uds_server(management, socks["management"])

        async def watchdog() -> None:
            # Stop if the egress process (our parent and only route out) has gone.
            misses = 0
            while misses < 3:
                await asyncio.sleep(2)
                misses = 0 if await asyncio.to_thread(egress.alive) else misses + 1
            log("cairnward core: egress process gone, shutting down")
            gw.should_exit = mgmt.should_exit = True

        async def run() -> None:
            dog = asyncio.create_task(watchdog())
            try:
                await asyncio.gather(
                    _serve_uds(gw, socks["gateway"]), _serve_uds(mgmt, socks["management"])
                )
            finally:
                dog.cancel()
                await backends.aclose()

        asyncio.run(run())


def serve(cfg: Config, *, with_egress: bool = True, log: Log) -> int:
    """Isolated mode when the privacy settings ask for it and it's available."""
    if cfg.privacy().value("isolation") == "on" and with_egress:
        try:
            return serve_isolated(cfg, log=log)
        except SandboxUnavailable as exc:
            log(f"isolation = on but inactive: {exc}. Running in a single process instead.")
    serve_single(cfg, with_egress=with_egress, log=log)
    return 0
