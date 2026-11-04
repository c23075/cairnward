"""`cairnward plugin`: install, pin, verify and run plugins (ADR-0017)."""

from __future__ import annotations

import json
import os
import subprocess
import sys
import tempfile
import tomllib
from pathlib import Path
from typing import Any

from cairnward.config.edit import _toml
from cairnward.config.loader import Config, ConfigError, load_config
from cairnward.config.models import McpServer
from cairnward.mcp.launch import LaunchError, plan_launch
from cairnward.plugins import conformance, contract


def _sandbox(cfg: Config) -> list[str] | None:
    from cairnward.sandbox import locate

    return locate(cfg.paths)


def _render(name: str, server: dict[str, Any]) -> str:
    out = [f"\n[servers.{name}]"]
    tables = {k: v for k, v in server.items() if isinstance(v, dict)}
    out += [f"{k} = {_toml(v)}" for k, v in server.items() if k not in tables]
    for key, table in tables.items():
        out.append(f"\n[servers.{name}.{key}]")
        out += [f"{k} = {_toml(v)}" for k, v in table.items()]
    return "\n".join(out) + "\n"


def describe(name: str, s: McpServer) -> list[str]:
    """What the user approves: what it may read, write and reach."""
    p = s.permissions
    return [
        f"plugin {name} {s.version}".rstrip(),
        f"  runs:        {' '.join(s.command)}",
        f"  may read:    {', '.join(p.read or []) or 'only its own folder'}",
        f"  may write:   {', '.join(p.write) or 'only its own folder'}",
        f"  fetch hosts: {', '.join(s.fetch_hosts) or 'none'}",
        f"  direct network (an exception): {', '.join(p.network) or 'none'}",
        f"  environments: {', '.join(s.environments) or 'none yet'}",
    ]


def add(cfg: Config, manifest: Path, *, yes: bool, read_input: Any = input) -> int:
    raw = tomllib.loads(manifest.read_text(encoding="utf-8"))
    name = raw.pop("name", None)
    if not isinstance(name, str):
        print("the manifest needs a name", file=sys.stderr)
        return 2
    if name in cfg.mcp.servers:
        print(f"'{name}' is already in mcp.toml; remove it first: cairnward plugin remove {name}",
              file=sys.stderr)  # fmt: skip
        return 2
    raw["kind"] = "plugin"
    if raw.get("pin_dir"):
        raw["sha256"] = contract.tree_digest(Path(raw["pin_dir"]).expanduser())
    server = McpServer.model_validate(raw)
    print("\n".join(describe(name, server)))
    if not yes and read_input("Add this plugin? [y/N] ").strip().lower() != "y":
        print("not added")
        return 1
    path = cfg.paths.mcp_config
    path.parent.mkdir(parents=True, exist_ok=True)
    if not path.exists():
        path.write_text("schema_version = 1\n", encoding="utf-8")
    with path.open("a", encoding="utf-8") as f:
        f.write(_render(name, raw))
    try:
        load_config(cfg.paths)
    except ConfigError as exc:  # leave the file as it was
        text = path.read_text(encoding="utf-8")
        path.write_text(text[: text.rindex(f"\n[servers.{name}]")], encoding="utf-8")
        print(f"not added: {exc}", file=sys.stderr)
        return 2
    print(f"added. Next: cairnward mcp approve {name}, then cairnward plugin verify {name}")
    return 0


def pin(cfg: Config, name: str) -> int:
    server = _plugin(cfg, name)
    if server.pin_dir is None:
        print(f"{name} has no pin_dir", file=sys.stderr)
        return 2
    digest = contract.tree_digest(server.pin_dir.expanduser())
    if digest == server.sha256:
        print(f"{name}: unchanged")
        return 0
    path = cfg.paths.mcp_config
    text = path.read_text(encoding="utf-8")
    if not server.sha256 or server.sha256 not in text:
        print(f"set sha256 = {digest!r} under [servers.{name}] in {path}", file=sys.stderr)
        return 2
    path.write_text(text.replace(server.sha256, digest), encoding="utf-8")
    print(f"{name}: pinned {digest[:12]}.... Run `cairnward plugin verify {name}` next")
    return 0


def _plugin(cfg: Config, name: str) -> McpServer:
    server = cfg.mcp.servers.get(name)
    if server is None or server.kind != "plugin":
        raise SystemExit(f"cairnward: no plugin called '{name}' (cairnward plugin list)")
    return server


def _environment(cfg: Config, name: str, wanted: str | None) -> str:
    on = [e for e, c in cfg.environments.items() if name in c.mcp_servers]
    if wanted:
        if wanted not in on:
            raise SystemExit(f"cairnward: {name} isn't on in '{wanted}'")
        return wanted
    if not on:
        raise SystemExit(f"cairnward: {name} isn't on in any environment (its `environments`)")
    return cfg.main.defaults.environment if cfg.main.defaults.environment in on else sorted(on)[0]


def _session(cfg: Config, name: str, environment: str) -> tuple[Any, dict[str, Any]]:
    from cairnward.run.runner import RunError, management

    try:
        mgmt = management(cfg)
    except RunError as exc:
        raise SystemExit(f"cairnward: {exc}") from None
    resp = mgmt.post("/v1/plugins/session", json={"plugin": name, "environment": environment})
    if resp.status_code != 200:
        raise SystemExit(f"cairnward: {resp.text[:300]}")
    return mgmt, resp.json()


def verify(cfg: Config, name: str, environment: str | None) -> int:
    server = _plugin(cfg, name)
    env = _environment(cfg, name, environment)
    if problem := contract.contract_problem(name, server) or contract.pin_problem(name, server):
        print(problem, file=sys.stderr)
        return 1
    mgmt, session = _session(cfg, name, env)
    try:
        results = conformance.verify(cfg, name, env, session["key"], session["gateway"],
                                     _sandbox(cfg))  # fmt: skip
    except LaunchError as exc:
        print(f"cairnward: {exc}", file=sys.stderr)
        return 1
    finally:
        mgmt.post(f"/v1/plugins/session/{session['run_id']}/end")
        mgmt.close()
    for r in results:
        print(f"  {'pass' if r.passed else 'FAIL'}  {r.name}"
              + (f": {r.detail}" if r.detail and not r.passed else ""))  # fmt: skip
    if all(r.passed for r in results):
        conformance.record_pass(cfg, name)
        print(f"{name} passed contract {contract.CONTRACT_VERSION}")
        return 0
    print(f"{name} failed: it won't be started", file=sys.stderr)
    return 1


def run(cfg: Config, name: str, environment: str | None, command: list[str]) -> int:
    """A command in the plugin's sandbox, with a key of its own and your terminal."""
    _plugin(cfg, name)
    env = _environment(cfg, name, environment)
    mgmt, session = _session(cfg, name, env)
    try:
        with tempfile.TemporaryDirectory(prefix=f"cw-plugin-{name}-") as tmp:
            try:
                plan = plan_launch(
                    cfg, name, env, srt=_sandbox(cfg), egress_url=None, tmp_dir=Path(tmp),
                    command=command or None,
                    extra_env={contract.ENV_KEY: session["key"],
                               contract.ENV_GATEWAY: session["gateway"],
                               contract.ENV_ENVIRONMENT: env,
                               contract.ENV_CONTRACT: str(contract.CONTRACT_VERSION)},
                )  # fmt: skip
            except LaunchError as exc:
                print(f"cairnward: {exc}", file=sys.stderr)
                return 1
            for warning in plan.warnings:
                print(f"cairnward: {warning}", file=sys.stderr)
            return subprocess.run(plan.argv, env=plan.env, cwd=plan.cwd,  # noqa: S603
                                  check=False).returncode  # fmt: skip
    finally:
        mgmt.post(f"/v1/plugins/session/{session['run_id']}/end")
        mgmt.close()


def allow_read(cfg: Config, name: str, folder: Path) -> int:
    """A folder you named for this plugin (a library's, say). A running server sees it after a
    restart; `cairnward plugin run` sees it at once."""
    server = _plugin(cfg, name)
    target = str(folder.expanduser().resolve())
    reads = list(server.permissions.read or [])
    if target in reads:
        return 0
    path = cfg.paths.mcp_config
    lines = path.read_text(encoding="utf-8").splitlines(keepends=True)
    start = next((i for i, x in enumerate(lines) if x.strip() == f"[servers.{name}.permissions]"),
                 None)  # fmt: skip
    if start is None:
        print(f"no [servers.{name}.permissions] in {path}", file=sys.stderr)
        return 2
    for i in range(start + 1, len(lines)):
        if lines[i].lstrip().startswith("read"):
            lines[i] = f"read = {_toml([*reads, target])}\n"
            break
        if lines[i].lstrip().startswith("["):
            print(f"no read list under [servers.{name}.permissions]", file=sys.stderr)
            return 2
    path.write_text("".join(lines), encoding="utf-8")
    print(f"{name} may now read {target}")
    return 0


def helper(cfg: Config, name: str, environment: str, command: list[str]) -> int:
    """A program that can't use the fetch service (rclone), run outside the sandbox with its
    traffic through egress in `environment`, whose network policy applies."""
    from cairnward.run.runner import RunError, egress_session

    _plugin(cfg, name)
    if not command:
        print("give a command after --", file=sys.stderr)
        return 2
    try:
        with egress_session(cfg, environment, f"plugin-{name}") as proxy:
            env = {**os.environ, "HTTP_PROXY": proxy, "HTTPS_PROXY": proxy}
            for key in ("NO_PROXY", "no_proxy", "ALL_PROXY", "all_proxy", "http_proxy",
                        "https_proxy"):  # fmt: skip
                env.pop(key, None)
            return subprocess.run(command, env=env, check=False).returncode  # noqa: S603
    except RunError as exc:
        print(f"cairnward: {exc}", file=sys.stderr)
        return 1


def list_plugins(cfg: Config, as_json: bool) -> int:
    rows = []
    for name, s in cfg.mcp.servers.items():
        if s.kind == "plugin":
            problem = contract.contract_problem(name, s) or contract.pin_problem(name, s)
            problem = problem or (None if s.unsandboxed else conformance.not_verified(cfg, name))
            rows.append({"name": name, "version": s.version, "fetch_hosts": s.fetch_hosts,
                         "environments": s.environments, "problem": problem})  # fmt: skip
    if as_json:
        print(json.dumps(rows, indent=2))
    elif not rows:
        print("no plugins (cairnward plugin add <manifest>)")
    for r in rows if not as_json else []:
        print(f"{r['name']} {r['version']}: {r['problem'] or 'ready'}")
    return 0


def remove(cfg: Config, name: str) -> int:
    """Takes it out of mcp.toml. Its data folder stays, since it may hold your work."""
    _plugin(cfg, name)
    path = cfg.paths.mcp_config
    kept: list[str] = []
    dropping = False
    for line in path.read_text(encoding="utf-8").splitlines(keepends=True):
        stripped = line.strip()
        if stripped.startswith("["):
            dropping = stripped.startswith((f"[servers.{name}]", f"[servers.{name}."))
        if not dropping:
            kept.append(line)
    path.write_text("".join(kept), encoding="utf-8")
    conformance.record_file(cfg, name).unlink(missing_ok=True)
    print(f"removed {name}. Its data stays in {cfg.paths.data_dir / 'mcp' / name}")
    return 0
