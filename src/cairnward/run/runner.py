"""`cairnward run`: start a coding agent inside the sandbox, pointed at the gateway.

1. Ask the running core (management socket) for a run: a short-lived gateway key and an
   egress session, both revoked when the run ends; `run.start` / `run.end` are audited.
2. Build a clean environment: a few basics plus what the agent profile needs.
3. With isolation on: wrap the command in srt (network only to the gateway and allowed
   hosts, via egress; writes only to the workspace; secrets and Cairnward's own data
   hidden). With isolation off: run directly (advisory: the agent could bypass egress).
"""

from __future__ import annotations

import os
import shutil
import subprocess
import tempfile
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import httpx

from cairnward.config.loader import Config
from cairnward.core.server import sockets
from cairnward.run.profiles import PROFILES, Profile, home_dirs
from cairnward.run.snapshots import SnapshotError, Snapshots
from cairnward.sandbox import (
    SandboxSpec,
    SandboxUnavailable,
    require,
    secret_dirs,
    srt_hosts,
    wrap,
    wrap_with_ask,
)
from cairnward.sandbox.srt import install_dir

BASE_ENV = (
    "PATH",
    "HOME",
    "TERM",
    "LANG",
    "LC_ALL",
    "USER",
    "LOGNAME",
    "SHELL",
    "TZ",
    "COLORTERM",
    "VIRTUAL_ENV",
)


class RunError(Exception):
    pass


def _ensure_dirs(dirs: list[Path]) -> list[Path]:
    """Agent state folders (e.g. ~/.qwen) must exist to be made writable in the sandbox."""
    for d in dirs:
        d.mkdir(parents=True, exist_ok=True)
    return dirs


@dataclass(frozen=True)
class Plan:
    """Everything needed to start the agent (built without side effects, for testing)."""

    argv: list[str]
    env: dict[str, str]
    sandboxed: bool
    spec: SandboxSpec | None
    warnings: list[str]


def management(cfg: Config) -> httpx.Client:
    sock = sockets(cfg)["management"]
    if not sock.exists():
        raise RunError("Cairnward isn't running: start it with `cairnward serve`")
    return httpx.Client(
        transport=httpx.HTTPTransport(uds=str(sock)), base_url="http://cairnward", timeout=30
    )


def plan_run(
    cfg: Config,
    *,
    environment: str,
    profile: Profile,
    model: str,
    workspace: Path,
    command: list[str],
    run: dict[str, Any],
    srt: list[str] | None,
    settings_file: Path,
    tmp_dir: Path,
    ask_socket: Path | None = None,
) -> Plan:
    eff = cfg.privacy(environment)
    gw_port, eg_port = run["gateway_port"], run["egress_port"]
    base_url = f"http://127.0.0.1:{gw_port}/v1"
    fast = f"cw/{environment}:low"
    env = {k: os.environ[k] for k in BASE_ENV if k in os.environ}
    env.update(profile.env(base_url, run["gateway_key"], model, fast))
    if profile.config is not None:
        env.update(profile.config(base_url, run["gateway_key"], model, fast, tmp_dir))
    env.update(CAIRNWARD_RUN_ID=run["run_id"], TMPDIR=str(tmp_dir))
    egress = (
        f"http://cw:{run['egress_token']}@127.0.0.1:{eg_port}" if run.get("egress_token") else None
    )
    warnings: list[str] = []

    if eff.value("isolation") == "on":
        if srt is None:
            raise RunError(
                "isolation = on but the sandbox isn't installed: run `cairnward "
                'sandbox install` (or set isolation = "off" for this environment)'
            )
        network = eff.value("network")
        hosts = [f"127.0.0.1:{gw_port}"]
        if network in ("allowlist", "ask", "open"):
            hosts += srt_hosts(eff.network_allow_hosts) + profile.extra_hosts
        argv = None
        if network in ("ask", "open") and ask_socket is not None:
            # other hosts: the sandbox asks Cairnward's policy (and so the user)
            argv = wrap_with_ask(srt, settings_file, command, ask_socket=ask_socket,
                                 run_id=run["run_id"], tmp_dir=tmp_dir)  # fmt: skip
        if argv is None and network == "open":
            warnings.append(
                "network = open, but the sandbox only reaches listed hosts (network_allow_hosts)"
            )
        if argv is None and network == "ask":
            warnings.append(
                "network = ask: this srt install can't ask (needs `cairnward sandbox install`); "
                "hosts not yet allowed are blocked"
            )
        elif network == "ask":
            warnings.append(
                "network = ask: answer requests for new hosts with `cairnward approvals`"
            )
        p = cfg.paths
        spec = SandboxSpec(
            allowed_hosts=hosts,
            allow_write=[workspace, tmp_dir, *_ensure_dirs(home_dirs(profile))],
            deny_read=[*secret_dirs(), p.config_dir, p.data_dir, p.state_dir],
            # srt's own helpers live in the data folder and must stay readable.
            allow_read=(install_dir(p),),
        )
        spec.write(settings_file)
        if egress:
            env.update(HTTP_PROXY=egress, HTTPS_PROXY=egress)  # srt's parent proxy
        if os.environ.get("CAIRNWARD_SANDBOX_DEBUG"):
            env["SRT_DEBUG"] = "1"  # srt logs every allow/deny decision to stderr
        argv = argv or wrap(srt, settings_file, command, tmp_dir=tmp_dir)
        return Plan(argv, env, True, spec, warnings)

    warnings.append("isolation = off: the agent runs unsandboxed and could bypass Cairnward")
    if egress:
        env.update(
            HTTP_PROXY=egress, HTTPS_PROXY=egress, NO_PROXY="127.0.0.1,localhost,::1"
        )  # the gateway is reached directly
    return Plan(command, env, False, None, warnings)


def run_agent(
    cfg: Config,
    *,
    command: list[str],
    environment: str | None = None,
    profile_name: str = "generic",
    model: str | None = None,
    workspace: Path | None = None,
    client: str | None = None,
    budget: dict[str, Any] | None = None,
    log: Any = print,
) -> int:
    if not command:
        raise RunError("nothing to run: cairnward run [options] -- <command> [args]")
    env_name = environment or cfg.main.defaults.environment
    if env_name not in cfg.environments:
        raise RunError(f"unknown environment '{env_name}'")
    profile = PROFILES.get(profile_name)
    if profile is None:
        raise RunError(f"unknown profile '{profile_name}' (known: {', '.join(PROFILES)})")
    if shutil.which(command[0]) is None and not Path(command[0]).exists():
        raise RunError(f"command not found: {command[0]}")
    env_cfg = cfg.environments[env_name]
    effort = env_cfg.effort_default or cfg.main.defaults.effort
    model = model or f"cw/{env_name}:{effort}"
    workspace = (workspace or Path.cwd()).resolve()
    srt = None
    if cfg.privacy(env_name).value("isolation") == "on":
        try:
            srt = require(cfg.paths)
        except SandboxUnavailable:
            srt = None

    with management(cfg) as mgmt:
        resp = mgmt.post(
            "/v1/runs",
            json={
                "environment": env_name,
                "client": client or profile.name,
                "profile": profile.name,
                "workspace": str(workspace),
                "program": Path(command[0]).name,
                "arg_count": len(command) - 1,
                "sandbox": "srt" if srt else "none",
                **{k: v for k, v in (budget or {}).items() if v},
            },
        )
        if resp.status_code != 200:
            raise RunError(f"Cairnward refused the run: {resp.text}")
        run = resp.json()
        started = time.monotonic()
        code = 1
        snaps: Snapshots | None = Snapshots(cfg.paths.state_dir, workspace)
        try:
            snaps.take(run["run_id"], "before")  # type: ignore[union-attr]
        except SnapshotError as exc:
            log(f"cairnward: no snapshot before the run ({exc}); runs diff/revert unavailable")
            snaps = None
        changed: int | None = None
        try:
            with tempfile.TemporaryDirectory(prefix="cairnward-run-") as tmp:
                plan = plan_run(
                    cfg,
                    environment=env_name,
                    profile=profile,
                    model=model,
                    workspace=workspace,
                    command=command,
                    run=run,
                    srt=srt,
                    settings_file=Path(tmp) / "srt.json",
                    tmp_dir=Path(tmp),
                    ask_socket=sockets(cfg)["management"],
                )
                for w in plan.warnings:
                    log(f"cairnward: {w}")
                log(
                    f"cairnward: run {run['run_id'][:8]} in '{env_name}' with {model} "
                    f"({'sandboxed' if plan.sandboxed else 'NOT sandboxed'})"
                )
                code = subprocess.run(
                    plan.argv, env=plan.env, cwd=workspace, check=False
                ).returncode
        finally:
            if snaps is not None:
                try:
                    snaps.take(run["run_id"], "after")
                    changed = len(snaps.changes(run["run_id"]))
                except SnapshotError as exc:
                    log(f"cairnward: no snapshot after the run ({exc})")
            mgmt.post(
                f"/v1/runs/{run['run_id']}/end",
                json={
                    "exit_code": code,
                    "duration_s": time.monotonic() - started,
                    "changed_files": changed,
                },
            )
    if changed:
        short = run["run_id"][:8]
        log(
            f"cairnward: {changed} file(s) changed. Review: cairnward runs diff {short}  "
            f"Undo: cairnward runs revert {short}"
        )
    return code
