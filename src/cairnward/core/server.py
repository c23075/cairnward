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
from cairnward.dashboard import DashboardAuth, dashboard_app
from cairnward.egress import EgressProxy, Session, Sessions
from cairnward.egress.control import EgressControl, LocalEgress, RemoteEgress, control_app
from cairnward.gateway import ClientKeys, GatewayDeps, create_app
from cairnward.gateway.clients import RunKeys
from cairnward.gateway.scheduling import BenchProfile, Scheduler
from cairnward.mcp.hub import McpHub
from cairnward.mcp.launch import BridgeLauncher, Launcher, LocalLauncher, RemoteLauncher
from cairnward.mcp.pins import PinStore
from cairnward.policy import ApprovalInbox, PolicyEngine, RemoteApprover
from cairnward.policy.provenance import ProvenanceTracker
from cairnward.research.engines.deep import RemoteExecutor, run_worker
from cairnward.research.online import Online
from cairnward.sandbox import SandboxSpec, SandboxUnavailable, require, secret_dirs, srt_hosts, wrap
from cairnward.store import Vault

Log = Callable[[str], None]
GRACE_S = 3  # in-flight requests (e.g. an approval still waiting) don't hold up a stop


def sockets(cfg: Config) -> dict[str, Path]:
    """Socket paths, in the state folder (kept at 0700: only the owner can reach them)."""
    s = cfg.paths.state_dir
    return {
        "management": s / "cairnward.sock",
        "gateway": s / "gateway.sock",
        "egress": s / "egress.sock",
        "dashboard": s / "dashboard.sock",
    }


def inbox_for(cfg: Config, log: Log) -> ApprovalInbox:
    """Pending approvals, answered with `cairnward approvals` (spec §5.3, M2)."""

    def notify(item: dict[str, object]) -> None:
        log(
            f"approval needed: {item['kind']} {item['target']} ({item['reason']}); "
            "answer with `cairnward approvals`"
        )

    return ApprovalInbox(cfg.main.egress.approval_timeout_s, notify=notify)


def _srt_if_wanted(cfg: Config) -> list[str] | None:
    """The sandbox command when any environment wants isolation and it's installed."""
    if not any(cfg.privacy(e).value("isolation") == "on" for e in [None, *cfg.environments]):
        return None
    try:
        return require(cfg.paths)
    except SandboxUnavailable:
        return None


async def retention_loop(cfg: Config, vault: Vault, log: Log) -> None:
    """Vault retention per environment and trash emptying, at start and then daily."""
    from cairnward.vault_cli import apply_retention

    while True:
        try:
            result = await asyncio.to_thread(apply_retention, cfg, vault)
            if total := sum(len(v) for v in result.values()):
                log(f"vault retention: purged {total} conversation(s)")
        except Exception as exc:  # never take the server down for this
            log(f"vault retention failed: {exc}")
        await asyncio.sleep(24 * 3600)


def core_background(cfg: Config, vault: Vault, hub: McpHub, log: Log) -> list[Any]:
    """Background work of the core, the same in both modes (isolated core, single process):
    one list so a mode can't silently miss one (the job runner once was)."""
    return [retention_loop(cfg, vault, log), hub.jobs.run_forever()]


async def backup_loop(cfg: Config, log: Log) -> None:
    """Backups (spec §4.8): with mode = "auto", a snapshot every interval_hours; in any
    mode with a repository, purged conversations are removed from old snapshots within
    the hour. Run by the process that can reach the backup folder (not the isolated core)."""
    from cairnward import backup

    b = cfg.main.backup
    if b.mode == "none" or not b.repository:
        return
    await asyncio.sleep(60)  # let startup settle
    while True:
        try:

            def once() -> dict[str, Any] | None:
                with backup.remote_access(cfg):  # a remote repository: through egress
                    if b.mode == "auto" and backup.due(cfg):
                        return backup.run(cfg, log)
                    if backup.pending(cfg):
                        backup.process_purges(cfg, log)
                return None

            if b.mode == "auto" or backup.pending(cfg):
                r = await asyncio.to_thread(once)
                if r:
                    log(f"backup {r['snapshot']}: {r['conversations']} conversations")
        except backup.BackupError as exc:
            log(f"backup: {exc}")
        except Exception as exc:  # a failed backup must not stop the server
            log(f"backup: unexpected error: {exc}")
        await asyncio.sleep(3600)


async def otel_loop(cfg: Config, audit: AuditLog, log: Log) -> None:
    """OpenTelemetry export (FR-AUD-5), when [telemetry] otlp_endpoint is set."""
    endpoint = cfg.main.telemetry.otlp_endpoint
    if not endpoint:
        return
    from cairnward.audit.otel import Exporter, OtelError

    try:
        exporter = Exporter(endpoint, cfg.paths.state_dir / "otel.json")
    except OtelError as exc:
        log(f"telemetry: {exc}")
        return
    reported = False
    while True:
        try:
            await asyncio.to_thread(exporter.export_once, audit)
            reported = False
        except OtelError as exc:
            if not reported:  # say it once, keep trying quietly
                log(f"telemetry: {exc}")
                reported = True
        await asyncio.sleep(cfg.main.telemetry.interval_s)


async def anchor_loop(cfg: Config, audit: AuditLog, log: Log) -> None:
    """audit_anchor = "git": push a signed checkpoint every interval (spec §5.2, D49)."""
    if cfg.privacy().value("audit_anchor") != "git":
        return
    from cairnward.audit.anchor import Anchorer, AnchorError

    if not cfg.main.anchor.repo:
        log('audit_anchor = "git" but no [anchor] repo is set: no checkpoints are pushed')
        return
    anchorer = Anchorer(cfg.paths, cfg.main.anchor)
    while True:
        try:
            payload = await asyncio.to_thread(anchorer.anchor, audit)
            if payload:
                audit.append("audit.anchor", actor="cairnward", outcome="ok",
                             target=cfg.main.anchor.repo,
                             details={"seq": payload["seq"], "hash": payload["hash"]})  # fmt: skip
        except (AnchorError, OSError) as exc:
            audit.append("audit.anchor", actor="cairnward", outcome="error",
                         details={"error": str(exc)[:300]})  # fmt: skip
            log(f"audit anchoring failed: {exc}")
        await asyncio.sleep(cfg.main.anchor.interval_minutes * 60)


def private_dir(path: Path) -> None:
    path.mkdir(parents=True, exist_ok=True)
    if sys.platform != "win32":
        path.chmod(0o700)


def _uds_server(app: Any, path: Path) -> uvicorn.Server:
    private_dir(path.parent)
    with contextlib.suppress(FileNotFoundError):
        path.unlink()
    return uvicorn.Server(
        uvicorn.Config(
            app,
            uds=str(path),
            log_level="warning",
            access_log=False,
            timeout_graceful_shutdown=GRACE_S,
        )
    )


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
    inbox: ApprovalInbox,
    backends: Backends,
    isolated: bool,
    launcher: Launcher,
    log: Log = lambda m: None,
    deep_executor: Any = None,
    docling_runner: Any = None,
) -> tuple[Any, Any, McpHub, Any]:
    engine = PolicyEngine(cfg, approver=inbox, audit=audit)
    run_keys = RunKeys()
    tracker = ProvenanceTracker()
    scheduler = Scheduler({n: b.max_parallel for n, b in cfg.backends.backends.items()})

    async def research_ask(library: str, question: str, **kw: Any) -> Any:
        from cairnward.research.ask import run_ask

        return await run_ask(cfg, engine, backends, library, question, scheduler=scheduler, **kw)

    def chat_for(library: str, effort: str | None = None) -> Any:
        """The library's environment's model; effort "low" picks its fast model (screening)."""
        from cairnward.research import Research
        from cairnward.research.llm import InProcessChat

        with Research.open(cfg.paths) as research:
            env = research.get(library).environment
        return InProcessChat(cfg, engine, backends, environment=env, scheduler=scheduler,
                             effort=effort, purpose="review")  # fmt: skip

    async def index_library(library: str, files: list[str] | None, actor: str) -> dict[str, Any]:
        from cairnward.research import InProcessEmbedder, Research

        with Research.open(cfg.paths) as research:
            env = research.get(library).environment
            embedder = InProcessEmbedder(cfg, engine, backends, environment=env, actor=actor)
            from cairnward.research.parsing import hooks

            parse_many, enrich = hooks(cfg, library, env, docling_runner=docling_runner,
                                       proxy=backends.web_proxy(env))  # fmt: skip
            report = await research.index(library, embedder, only=files, parse_many=parse_many,
                                          enrich=enrich)  # fmt: skip
        fields = ("added", "updated", "unchanged", "no_text", "failed", "missing", "chunks",
                  "seconds")  # fmt: skip
        details = {k: getattr(report, k) for k in fields}
        audit.append("library.index", actor=actor, environment=env, target=library,
                     details=details | {"files": len(files) if files else None})  # fmt: skip
        return details

    from cairnward.research import Research
    from cairnward.research.engines.deep import DeepEngine, LocalExecutor
    from cairnward.research.jobs import JobRunner, JobStore

    deep = DeepEngine(cfg, run_keys=run_keys, audit=audit,
                      executor=deep_executor or LocalExecutor(cfg, log))  # fmt: skip
    jobs = JobRunner(JobStore(cfg.paths.data_dir), research=lambda: Research.open(cfg.paths),
                     ask=research_ask, chat_for=chat_for, audit=audit, log=log,
                     deep=deep, index=index_library, paths=cfg.paths,
                     cite_engine=cfg.main.research.citation_engine)  # fmt: skip

    online = Online(cfg, engine, backends)
    hub = McpHub(cfg, engine, launcher, PinStore.at(cfg.paths.data_dir), audit,
                 provenance=tracker, vault=vault, backends=backends,
                 research_ask=research_ask, jobs=jobs, online=online)  # fmt: skip
    gateway = create_app(
        GatewayDeps(
            cfg,
            engine,
            backends,
            ClientKeys.at(cfg.paths.state_dir),
            vault,
            run_keys,
            profile=BenchProfile.at(cfg.paths.state_dir),
            mcp=hub,
            provenance=tracker,
            scheduler=scheduler,
        )
    )
    auth = DashboardAuth()
    dashboard = dashboard_app(cfg, auth=auth, audit=audit, vault=vault, inbox=inbox, mcp=hub,
                              isolated=isolated, jobs=jobs)  # fmt: skip
    management = management_app(
        cfg,
        audit=audit,
        run_keys=run_keys,
        egress=egress,
        isolated=isolated,
        engine=engine,
        inbox=inbox,
        mcp=hub,
        vault=vault,
        dashboard=auth,
        research_ask=research_ask,
        jobs=jobs,
        online=online,
    )
    return gateway, management, hub, dashboard


# ------------------------------------------------------------------ single process


def serve_single(cfg: Config, *, with_egress: bool, log: Log) -> None:
    server = cfg.main.server
    socks = sockets(cfg)
    with AuditLog.open(cfg.paths, enabled_for(cfg)) as audit, Vault.open(cfg.paths) as vault:
        sync = record_settings(cfg, audit)
        inbox = inbox_for(cfg, log)
        sessions = Sessions()
        egress_ctl = LocalEgress(sessions) if with_egress else None
        # One trusted session per environment: the core's own connections show under the
        # environment they were checked in, and egress applies that environment's explicit
        # allow-list entries (e.g. a server on this machine).
        core_tokens: dict[str, str] = {}

        def egress_url_for(environment: str | None) -> str | None:
            if not with_egress:
                return None
            name = environment if environment in cfg.environments else cfg.main.defaults.environment
            if name not in core_tokens:
                core_tokens[name] = sessions.create(Session(environment=name, trusted_core=True))
            return f"http://cw:{core_tokens[name]}@127.0.0.1:{server.egress_port}"

        backends = Backends(cfg.backends, egress_url_for=egress_url_for)
        launcher = LocalLauncher(
            cfg,
            srt=_srt_if_wanted(cfg),
            create_session=sessions.create,
            revoke_session=sessions.revoke,
            egress_port=server.egress_port if with_egress else None,
        )
        gateway, management, hub, dashboard = _core_parts(
            cfg,
            audit,
            vault,
            egress_ctl,
            inbox=inbox,
            backends=backends,
            isolated=False,
            launcher=launcher,
            log=log,
        )
        gw = uvicorn.Server(
            uvicorn.Config(
                gateway,
                host=server.gateway_host,
                port=server.gateway_port,
                log_level="warning",
                access_log=False,
                timeout_graceful_shutdown=GRACE_S,
            )
        )
        mgmt = _uds_server(management, socks["management"])
        dash = uvicorn.Server(
            uvicorn.Config(
                dashboard,
                host=server.dashboard_host,
                port=server.dashboard_port,
                log_level="warning",
                access_log=False,
                timeout_graceful_shutdown=GRACE_S,
            )
        )

        async def run() -> None:
            tasks = [
                dash.serve(),
                gw.serve(),
                _serve_uds(mgmt, socks["management"]),
                anchor_loop(cfg, audit, log),
                backup_loop(cfg, log),
                otel_loop(cfg, audit, log),
                *core_background(cfg, vault, hub, log),
            ]
            if with_egress:
                proxy = EgressProxy(cfg, PolicyEngine(cfg, approver=inbox, audit=audit), sessions)
                await proxy.start()
                tasks.append(proxy.serve_forever())
                log(f"cairnward-egress on 127.0.0.1:{proxy.port}")
            log(
                f"Cairnward gateway on http://{server.gateway_host}:{server.gateway_port}/v1 "
                f"(privacy: {cfg.privacy().label}; single process, core not isolated)"
            )
            log("dashboard: run `cairnward dashboard` to open it")
            gw_host, gw_port = server.gateway_host, server.gateway_port
            await _client_socket(cfg, lambda: asyncio.open_connection(gw_host, gw_port))
            if sync.changes:
                log(f"Recorded {len(sync.changes)} settings change(s) in the audit log.")
            try:
                await asyncio.gather(*tasks)
            finally:
                inbox.close()
                await hub.aclose()
                await backends.aclose()

        asyncio.run(run())


# ------------------------------------------------------------------ isolated: process A


Opener = Callable[[], Any]  # returns an awaitable (reader, writer) pair


def _relay(open_upstream: Opener) -> Callable[[asyncio.StreamReader, asyncio.StreamWriter], Any]:
    async def handle(reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        try:
            up_r, up_w = await open_upstream()
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

    return handle


async def _forward(local_port: int, uds: Path) -> asyncio.Server:
    """127.0.0.1:<port> -> a Unix socket (the isolated core's gateway or dashboard)."""
    opener = lambda: asyncio.open_unix_connection(str(uds))  # noqa: E731
    return await asyncio.start_server(_relay(opener), "127.0.0.1", local_port)


def client_socket(cfg: Config) -> Path:
    """The gateway as a Unix socket for clients in containers (FR-ISO-4): mount this
    folder into a client's sidecar instead of giving the client a network route."""
    return cfg.paths.state_dir / "clients" / "gateway.sock"


async def _client_socket(cfg: Config, opener: Opener) -> asyncio.Server | None:
    if sys.platform == "win32":
        return None
    path = client_socket(cfg)
    private_dir(path.parent)
    with contextlib.suppress(FileNotFoundError):
        path.unlink()
    srv = await asyncio.start_unix_server(_relay(opener), str(path))
    if sys.platform != "win32":
        path.chmod(0o600)
    return srv


class RemoteDocling:
    """The isolated core asks the outer process to run Docling (srt can't nest)."""

    def __init__(self, control_socket: str) -> None:
        self.socket = control_socket

    async def __call__(self, environment: str, files: list[Path]) -> list[dict[str, Any]]:
        import httpx

        from cairnward.research.engines.docling import DoclingError

        transport = httpx.AsyncHTTPTransport(uds=self.socket)
        async with httpx.AsyncClient(transport=transport, base_url="http://egress",
                                     timeout=httpx.Timeout(None, connect=10)) as c:  # fmt: skip
            resp = await c.post(
                "/v1/engines/docling/parse",
                json={"environment": environment, "files": [str(f) for f in files]},
            )
        if resp.status_code != 200:
            raise DoclingError(resp.json().get("detail", resp.text))
        results: list[dict[str, Any]] = resp.json()["results"]
        return results


def core_sandbox(cfg: Config) -> SandboxSpec:
    """What the core may reach: local model servers and backend hosts (handed to egress),
    and the GROBID service when one is set."""
    hosts = []
    urls = [b.url for b in cfg.backends.backends.values()]
    if cfg.main.research.grobid_url:
        urls.append(cfg.main.research.grobid_url)
    for url in urls:
        u = urlsplit(url)
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
        # Asks go to the inbox in the core (its management socket).
        asker = RemoteApprover(str(socks["management"]), cfg.main.egress.approval_timeout_s)
        engine = PolicyEngine(cfg, approver=asker, audit=audit)
        proxy = EgressProxy(cfg, engine, sessions)
        bridge = BridgeLauncher(
            LocalLauncher(
                cfg,
                srt=srt,
                create_session=sessions.create,
                revoke_session=sessions.revoke,
                egress_port=server.egress_port,
            ),
            cfg.paths.state_dir / "mcp",
        )

        async def deep(**kw: Any) -> dict[str, Any]:
            return await asyncio.to_thread(run_worker, cfg, log=log, **kw)

        async def docling(environment: str, files: list[Path]) -> list[dict[str, Any]]:
            from cairnward.research.engines import docling as dl

            return await asyncio.to_thread(dl.run_worker, cfg, environment, files, log=log)

        ctl = _uds_server(control_app(sessions, mcp=bridge, deep=deep, docling=docling),
                          socks["egress"])  # fmt: skip

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
                fwd_dash = await _forward(server.dashboard_port, socks["dashboard"])
                gw_sock = str(socks["gateway"])
                fwd_clients = await _client_socket(
                    cfg, lambda: asyncio.open_unix_connection(gw_sock)
                )
                core = await asyncio.create_subprocess_exec(*cmd, env=env, stdin=subprocess.DEVNULL)
                log(f"cairnward-egress on 127.0.0.1:{proxy.port} (only networked process)")
                log(
                    f"Cairnward gateway on http://127.0.0.1:{server.gateway_port}/v1 "
                    f"(privacy: {cfg.privacy().label}; core isolated, pid {core.pid})"
                )
                serve_task = asyncio.create_task(proxy.serve_forever())
                anchors = asyncio.create_task(anchor_loop(cfg, audit, log))
                backups = asyncio.create_task(backup_loop(cfg, log))
                telemetry = asyncio.create_task(otel_loop(cfg, audit, log))
                try:
                    return await core.wait()
                finally:
                    if core.returncode is None:
                        core.terminate()
                        with contextlib.suppress(Exception):
                            await asyncio.wait_for(core.wait(), 10)
                    fwd.close()
                    fwd_dash.close()
                    if fwd_clients:
                        fwd_clients.close()
                    ctl.should_exit = True
                    serve_task.cancel()
                    anchors.cancel()
                    backups.cancel()
                    telemetry.cancel()
                    await bridge.aclose()
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
        backends = Backends(cfg.backends, proxy_all=srt_proxy or None)
        egress = RemoteEgress(str(egress_control))
        inbox = inbox_for(cfg, log)
        gateway, management, hub, dashboard = _core_parts(
            cfg,
            audit,
            vault,
            egress,
            inbox=inbox,
            backends=backends,
            isolated=True,
            launcher=RemoteLauncher(str(egress_control)),
            log=log,
            deep_executor=RemoteExecutor(str(egress_control)),
            docling_runner=RemoteDocling(str(egress_control)),
        )
        gw = _uds_server(gateway, socks["gateway"])
        mgmt = _uds_server(management, socks["management"])
        dash = _uds_server(dashboard, socks["dashboard"])

        async def watchdog() -> None:
            # Stop if the egress process (our parent and only route out) has gone.
            misses = 0
            while misses < 3:
                await asyncio.sleep(2)
                misses = 0 if await asyncio.to_thread(egress.alive) else misses + 1
            log("cairnward core: egress process gone, shutting down")
            gw.should_exit = mgmt.should_exit = dash.should_exit = True

        async def run() -> None:
            dog = asyncio.create_task(watchdog())
            background = [asyncio.create_task(c) for c in core_background(cfg, vault, hub, log)]
            try:
                await asyncio.gather(
                    _serve_uds(gw, socks["gateway"]),
                    _serve_uds(mgmt, socks["management"]),
                    _serve_uds(dash, socks["dashboard"]),
                )
            finally:
                dog.cancel()
                for task in background:
                    task.cancel()
                inbox.close()
                await hub.aclose()
                await backends.aclose()

        asyncio.run(run())


SOCKET_PATH_MAX = 107  # sun_path on Linux (108 bytes with the terminating NUL); macOS: 103


def socket_path_problem(cfg: Config) -> str | None:
    """The longest Unix socket path Cairnward makes, if it's too long to bind."""
    longest = cfg.paths.state_dir / "mcp" / f"mcp-{'0' * 16}.sock"
    limit = 103 if sys.platform == "darwin" else SOCKET_PATH_MAX
    if os.name != "nt" and len(os.fsencode(longest)) > limit:
        return (
            f"the state folder's path is too long for Unix sockets ({len(os.fsencode(longest))}"
            f" > {limit} bytes for {longest}); set CAIRNWARD_STATE_DIR to a shorter path, "
            "e.g. ~/.local/state/cairnward"
        )
    return None


def serve(cfg: Config, *, with_egress: bool = True, log: Log) -> int:
    """Isolated mode when the privacy settings ask for it and it's available."""
    if problem := socket_path_problem(cfg):
        log(f"cairnward: can't start: {problem}")
        return 2
    if cfg.privacy().value("isolation") == "on" and with_egress:
        try:
            return serve_isolated(cfg, log=log)
        except SandboxUnavailable as exc:
            log(f"isolation = on but inactive: {exc}. Running in a single process instead.")
    serve_single(cfg, with_egress=with_egress, log=log)
    return 0
