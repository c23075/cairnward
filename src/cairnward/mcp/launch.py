"""Starting third-party MCP servers in their own sandbox (spec §4.2, FR-MCP-2).

A server gets the intersection of its manifest (mcp.toml `permissions`) and the
environment's privacy settings:
- network: only the manifest's hosts, and only through egress, which applies the
  environment's network policy on top (network = off: no hosts at all). Addresses of this
  machine are dropped unless listed exactly in network_allow_hosts (the sandbox would
  reach them directly, around egress);
- files: writes only inside the environment's workspace or the server's own folder
  ({data}, which is also its HOME); reads everywhere except secrets and Cairnward's own
  folders, or only the listed folders when the manifest sets `read`;
- environment variables: a few basics, the fixed `env`, and those the manifest names.

Isolation off: the server runs unsandboxed (proxy settings are advisory), with a warning.

Where servers start: in single-process mode the core starts them. In isolated mode the
core is itself in a sandbox (srt can't nest), so the networked outer process starts them
from its own copy of the configuration (the core only names a server and environment)
and hands the core a Unix socket connected to the server's stdio.
"""

from __future__ import annotations

import asyncio
import contextlib
import hashlib
import os
import secrets
import shutil
import tempfile
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Protocol

import httpx

from cairnward.config.loader import Config
from cairnward.egress.addresses import literal_address, special_reason
from cairnward.egress.proxy import Session
from cairnward.mcp.jsonrpc import MAX_MESSAGE
from cairnward.policy.engine import host_matches
from cairnward.sandbox import SandboxSpec, secret_dirs, srt_hosts, wrap
from cairnward.sandbox.srt import srt_dirs

BASE_ENV = ("PATH", "LANG", "LC_ALL", "TZ", "TERM")


class LaunchError(Exception):
    pass


def server_dir(cfg: Config, server: str) -> Path:
    return cfg.paths.data_dir / "mcp" / server


@dataclass(frozen=True)
class LaunchPlan:
    argv: list[str]
    env: dict[str, str]
    cwd: Path
    log: Path
    sandboxed: bool
    spec: SandboxSpec | None
    network: list[str]
    warnings: list[str] = field(default_factory=list)


def _inside(path: Path, roots: list[Path]) -> bool:
    return any(path == r or path.is_relative_to(r) for r in roots)


def plan_launch(
    cfg: Config,
    server_name: str,
    environment: str,
    *,
    srt: list[str] | None,
    egress_url: str | None,
    tmp_dir: Path,
    extra_env: dict[str, str] | None = None,
    command: list[str] | None = None,
    verifying: bool = False,
    extra_read: list[Path] | None = None,
) -> LaunchPlan:
    """extra_env: a plugin's key and service addresses. command: instead of the server's own
    (`cairnward plugin run`, and the conformance suite's probe). verifying: the suite itself,
    which can't require its own earlier pass."""
    server = cfg.mcp.servers.get(server_name)
    env_cfg = cfg.environments.get(environment)
    if server is None or env_cfg is None or server_name not in env_cfg.mcp_servers:
        raise LaunchError(f"MCP server '{server_name}' isn't enabled in '{environment}'")
    plugin = server.kind == "plugin"
    if plugin:
        from cairnward.plugins import conformance, contract

        problem = contract.contract_problem(server_name, server)
        if problem is None and not server.unsandboxed:
            problem = contract.pin_problem(server_name, server)
        if problem is None and not verifying and not server.unsandboxed:
            problem = conformance.not_verified(cfg, server_name)
        if problem:
            raise LaunchError(problem)
    if server.pin_file is not None:
        try:
            digest = hashlib.sha256(server.pin_file.expanduser().read_bytes()).hexdigest()
        except OSError as exc:
            raise LaunchError(f"{server_name}: can't read pin_file: {exc}") from None
        if digest != server.sha256:
            raise LaunchError(
                f"{server_name}: {server.pin_file} changed (sha256 {digest[:12]}..., pinned "
                f"{(server.sha256 or '')[:12]}...): not starting it; review and update mcp.toml"
            )
    data = server_dir(cfg, server_name)
    home = data / "home"
    home.mkdir(parents=True, exist_ok=True)
    for d in (data.parent, data):
        d.chmod(0o700)
    roots = [w.resolve() for w in env_cfg.workspace]
    workspace = roots[0] if roots else None

    def expand(raw: str) -> Path:
        if "{workspace}" in raw and workspace is None:
            raise LaunchError(f"{server_name}: uses {{workspace}} but '{environment}' has none")
        text = raw.replace("{workspace}", str(workspace)).replace("{data}", str(data))
        return Path(text).expanduser().resolve()

    perms = server.permissions
    writes = [expand(w) for w in perms.write]
    for w in writes:
        if plugin and not _inside(w, [data]):
            raise LaunchError(f"{server_name}: a plugin may only write inside {{data}}, not {w}")
        if not _inside(w, [*roots, data]):
            raise LaunchError(
                f"{server_name}: may only write inside the workspace of '{environment}' or "
                f"{{data}}, not {w}"
            )
    reads = [expand(r) for r in perms.read] if perms.read is not None else None
    cwd = expand(server.cwd)
    eff = cfg.privacy(environment)
    warnings: list[str] = []

    hosts: list[str] = []
    if plugin:  # checked in mcp.toml: exact local addresses, approved at install
        if eff.value("network") != "off":
            hosts += perms.network
            warnings += [f"direct network access to {h}" for h in perms.network]
        elif perms.network:
            warnings.append(f"network = off in '{environment}': no direct access for {server_name}")
    elif eff.value("network") != "off":
        for h in perms.network:
            name = h.rsplit(":", 1)[0] if h.count(":") == 1 else h
            ip = literal_address(name.strip("[]"))
            if (
                ip
                and special_reason(ip)
                and not any(host_matches(h, p) for p in eff.network_allow_hosts)
            ):
                warnings.append(f"{h} is this machine or a special address: dropped")
                continue
            hosts.append(h)
    elif perms.network:
        warnings.append(f"network = off in '{environment}': {server_name} gets no network")

    run_env = {k: os.environ[k] for k in BASE_ENV if k in os.environ}
    run_env.update(HOME=str(home), TMPDIR=str(tmp_dir))
    run_env.update(server.env)
    for name in perms.env:
        if name in os.environ:
            run_env[name] = os.environ[name]
    run_env.update(extra_env or {})
    if plugin:
        from cairnward.plugins.contract import ENV_DATA

        run_env[ENV_DATA] = str(data)
    if egress_url and hosts and not plugin:
        run_env.update(HTTP_PROXY=egress_url, HTTPS_PROXY=egress_url,
                       http_proxy=egress_url, https_proxy=egress_url)  # fmt: skip
    log = cfg.paths.state_dir / "logs" / f"mcp-{server_name}-{environment}.log"
    log.parent.mkdir(parents=True, exist_ok=True)

    program = list(command or server.command)
    if plugin and server.unsandboxed:
        warnings.append(f"{server_name} is a plugin running outside the sandbox (unsandboxed)")
        return LaunchPlan(program, run_env, cwd, log, False, None, hosts, warnings)
    if eff.value("isolation") != "on" and not plugin:
        warnings.append("isolation = off: the MCP server runs unsandboxed")
        return LaunchPlan(program, run_env, cwd, log, False, None, hosts, warnings)
    if srt is None:
        raise LaunchError(
            "isolation = on but the sandbox isn't installed: `cairnward sandbox install`"
        )
    p = cfg.paths
    deny = [*secret_dirs(), p.config_dir, p.data_dir, p.state_dir]
    allow_read: list[Path] = [*srt_dirs(p, srt), data, *(extra_read or [])]
    if reads is not None:
        deny.append(Path.home())
        allow_read += reads
    allowed = srt_hosts(hosts)
    if plugin:
        allowed.append(f"127.0.0.1:{cfg.main.server.gateway_port}")  # the plugin services
        if server.pin_dir is not None:
            allow_read.append(server.pin_dir.expanduser())
        from cairnward.run.runner import program_reads

        allow_read += program_reads(program, run_env.get("PATH", ""), workspace=cwd,
                                    home=Path.home())  # fmt: skip
    spec = SandboxSpec(
        allowed_hosts=allowed,
        allow_write=[*writes, data, tmp_dir],
        deny_read=deny,
        allow_read=tuple(allow_read),
    )
    settings = spec.write(tmp_dir / "srt.json")
    argv = wrap(srt, settings, program, tmp_dir=tmp_dir)
    return LaunchPlan(argv, run_env, cwd, log, True, spec, hosts, warnings)


# ------------------------------------------------------------------ launchers


@dataclass
class Started:
    reader: asyncio.StreamReader
    writer: asyncio.StreamWriter
    stop: Callable[[], Awaitable[None]]
    sandboxed: bool
    warnings: list[str]


class Launcher(Protocol):
    async def start(self, server: str, environment: str,
                    extra_env: dict[str, str] | None = None) -> Started: ...  # fmt: skip


class LocalLauncher:
    """Starts servers as child processes of this process."""

    def __init__(
        self,
        cfg: Config,
        *,
        srt: list[str] | None,
        create_session: Callable[[Session], str] | None = None,
        revoke_session: Callable[[str], None] | None = None,
        egress_port: int | None = None,
    ):
        self.cfg, self.srt = cfg, srt
        self._create, self._revoke, self._port = create_session, revoke_session, egress_port

    async def start(self, server: str, environment: str,
                    extra_env: dict[str, str] | None = None) -> Started:  # fmt: skip
        tmp = Path(tempfile.mkdtemp(prefix=f"cairnward-mcp-{server}-"))
        token = None
        if self._create is not None and self._port:
            token = self._create(Session(environment=environment, client=f"mcp:{server}",
                                         actor=f"mcp:{server}"))  # fmt: skip
        egress_url = f"http://cw:{token}@127.0.0.1:{self._port}" if token else None
        try:
            plan = plan_launch(self.cfg, server, environment, srt=self.srt,
                               egress_url=egress_url, tmp_dir=tmp,
                               extra_env=extra_env)  # fmt: skip
            with plan.log.open("ab") as log:
                proc = await asyncio.create_subprocess_exec(
                    *plan.argv, stdin=asyncio.subprocess.PIPE, stdout=asyncio.subprocess.PIPE,
                    stderr=log, env=plan.env, cwd=plan.cwd, limit=MAX_MESSAGE,
                    start_new_session=True,
                )  # fmt: skip
        except BaseException:
            if token and self._revoke:
                self._revoke(token)
            shutil.rmtree(tmp, ignore_errors=True)
            raise
        if proc.stdout is None or proc.stdin is None:
            raise LaunchError("no pipes to the MCP server")

        async def stop() -> None:
            if proc.returncode is None:
                with contextlib.suppress(ProcessLookupError):
                    os.killpg(proc.pid, 15)
                try:
                    await asyncio.wait_for(proc.wait(), 5)
                except TimeoutError:
                    with contextlib.suppress(ProcessLookupError):
                        os.killpg(proc.pid, 9)
                    await proc.wait()
            if token and self._revoke:
                self._revoke(token)
            shutil.rmtree(tmp, ignore_errors=True)

        return Started(proc.stdout, proc.stdin, stop, plan.sandboxed, plan.warnings)


class BridgeLauncher:
    """Isolated mode, outer process: start a server and expose its stdio on a fresh
    owner-only Unix socket for the core (one connection; closing it stops the server)."""

    def __init__(self, local: LocalLauncher, sock_dir: Path):
        self.local, self.sock_dir = local, sock_dir
        self._running: dict[str, Callable[[], Awaitable[None]]] = {}

    async def start(self, server: str, environment: str,
                    extra_env: dict[str, str] | None = None) -> dict[str, Any]:  # fmt: skip
        started = await self.local.start(server, environment, extra_env)
        self.sock_dir.mkdir(parents=True, exist_ok=True)
        self.sock_dir.chmod(0o700)
        path = self.sock_dir / f"mcp-{secrets.token_hex(8)}.sock"
        key = str(path)
        connected = asyncio.Event()

        async def handle(reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
            if connected.is_set():  # one client only
                writer.close()
                return
            connected.set()

            async def pipe(src: asyncio.StreamReader, dst: asyncio.StreamWriter) -> None:
                try:
                    while chunk := await src.read(65536):
                        dst.write(chunk)
                        await dst.drain()
                except (ConnectionError, OSError):
                    pass
                finally:
                    with contextlib.suppress(Exception):
                        dst.close()

            await asyncio.wait(
                [asyncio.create_task(pipe(reader, started.writer)),
                 asyncio.create_task(pipe(started.reader, writer))],
                return_when=asyncio.FIRST_COMPLETED,
            )  # fmt: skip
            await shutdown()

        srv = await asyncio.start_unix_server(handle, str(path), limit=MAX_MESSAGE)
        path.chmod(0o600)

        async def shutdown() -> None:
            if self._running.pop(key, None) is None:
                return
            srv.close()
            path.unlink(missing_ok=True)
            await started.stop()

        self._running[key] = shutdown

        async def unclaimed() -> None:
            await asyncio.sleep(30)
            if not connected.is_set():
                await shutdown()

        asyncio.get_running_loop().create_task(unclaimed())
        return {"socket": key, "sandboxed": started.sandboxed, "warnings": started.warnings}

    async def aclose(self) -> None:
        for stop in list(self._running.values()):
            with contextlib.suppress(Exception):
                await stop()


class RemoteLauncher:
    """Isolated mode, core side: ask the outer process (egress control socket)."""

    def __init__(self, control_socket: str):
        self._socket = control_socket

    async def start(self, server: str, environment: str,
                    extra_env: dict[str, str] | None = None) -> Started:  # fmt: skip
        transport = httpx.AsyncHTTPTransport(uds=self._socket)
        async with httpx.AsyncClient(transport=transport, base_url="http://egress",
                                     timeout=120) as c:  # fmt: skip
            resp = await c.post("/v1/mcp/start", json={"server": server,
                                                       "environment": environment,
                                                       "extra_env": extra_env or {}})  # fmt: skip
        if resp.status_code != 200:
            raise LaunchError(resp.json().get("detail", resp.text))
        info = resp.json()
        reader, writer = await asyncio.open_unix_connection(info["socket"], limit=MAX_MESSAGE)

        async def stop() -> None:
            with contextlib.suppress(Exception):
                writer.close()

        return Started(reader, writer, stop, bool(info["sandboxed"]), list(info["warnings"]))
