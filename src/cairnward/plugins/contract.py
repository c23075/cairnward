"""What the core and a plugin agree on (ADR-0017): the contract version, the environment
variables a plugin is started with, and how its installed code is pinned."""

from __future__ import annotations

import hashlib
from pathlib import Path

from cairnward.config.models import McpConfig, McpServer

CONTRACT_VERSION = 1
ENV_KEY = "CAIRNWARD_PLUGIN_KEY"
ENV_GATEWAY = "CAIRNWARD_GATEWAY_URL"
ENV_ENVIRONMENT = "CAIRNWARD_ENVIRONMENT"
ENV_CONTRACT = "CAIRNWARD_CONTRACT"
ENV_DATA = "CAIRNWARD_PLUGIN_DATA"

SKIPPED = {"__pycache__"}


def tree_digest(folder: Path) -> str:
    """SHA-256 over every file's relative path and contents. Links count as their target's
    path, so re-pointing one changes the digest."""
    outer = hashlib.sha256()
    for path in sorted(folder.rglob("*")):
        if SKIPPED & set(path.relative_to(folder).parts):
            continue
        rel = path.relative_to(folder).as_posix()
        if path.is_symlink():
            outer.update(f"L {rel} {path.readlink()}\n".encode())
        elif path.is_file():
            outer.update(f"F {rel} {hashlib.sha256(path.read_bytes()).hexdigest()}\n".encode())
    return outer.hexdigest()


def weakenings(cfg_mcp: McpConfig) -> list[str]:
    """Plugin protections switched off, or exceptions granted: shown and audited (D55)."""
    out = []
    for name, s in cfg_mcp.servers.items():
        if s.kind != "plugin":
            continue
        if s.unsandboxed:
            out.append(f"plugin {name} runs outside the sandbox, with its code unpinned")
        if s.unpinned_tools:
            out.append(f"plugin {name}: changed tool definitions aren't held for review")
        if s.permissions.network:
            out.append(f"plugin {name} has direct network access: "
                       f"{', '.join(s.permissions.network)}")  # fmt: skip
    return out


def contract_problem(name: str, server: McpServer, core: int = CONTRACT_VERSION) -> str | None:
    if not server.contract_min <= core <= server.contract_max:
        return (f"{name} supports contract {server.contract_min} to {server.contract_max}, and "
                f"this Cairnward has contract {core}: update one of them")  # fmt: skip
    return None


def pin_problem(name: str, server: McpServer) -> str | None:
    """Why this plugin's installed code may not start (rule 4), or None."""
    if server.pin_dir is None:
        return None if server.unsandboxed else f"{name}: a plugin needs pin_dir and sha256"
    folder = server.pin_dir.expanduser()
    if not folder.is_dir():
        return f"{name}: pin_dir {folder} isn't a folder"
    digest = tree_digest(folder)
    if digest != server.sha256:
        return (f"{name}: its installed code changed (sha256 {digest[:12]}..., pinned "
                f"{(server.sha256 or '')[:12]}...): not starting it. Review the change, then run "
                f"`cairnward plugin pin {name}`")  # fmt: skip
    return None
