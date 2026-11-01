"""Anthropic sandbox-runtime (srt) integration (ADR-0013).

srt wraps bubblewrap, seccomp and filtering proxies. Cairnward:
- locates a pinned srt (`cairnward sandbox install`, $CAIRNWARD_SRT, or `srt` on PATH);
- builds srt settings from the effective privacy policy (srt refuses catch-all allow
  lists, so hosts are listed explicitly);
- clears `no_proxy` inside the sandbox, so programs reach Cairnward's gateway on the
  host loopback through srt's proxy instead of their private loopback.
"""

from __future__ import annotations

import json
import os
import re
import shlex
import shutil
import subprocess
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from cairnward.config.paths import Paths

SRT_VERSION = "0.0.81"
MIN_NODE = (22, 12)
MIN_NODE_TEXT = ".".join(map(str, MIN_NODE))
PKG_DIR = Path(__file__).parent / "srt_pkg"
CLI_REL = Path("node_modules/@anthropic-ai/sandbox-runtime/dist/cli.js")

# Never readable from inside a sandbox (relative to the home directory).
SECRET_DIRS = (
    ".ssh",
    ".gnupg",
    ".aws",
    ".azure",
    ".config/gcloud",
    ".kube",
    ".docker",
    ".password-store",
    ".local/share/keyrings",
    ".mozilla",
    ".config/google-chrome",
    ".config/chromium",
    ".netrc",
    ".git-credentials",
    ".config/gh",
)


class SandboxUnavailable(Exception):
    pass


def install_dir(paths: Paths) -> Path:
    return paths.data_dir / "sandbox-runtime"


def node_version(node: str = "node") -> tuple[int, int] | None:
    exe = shutil.which(node)
    if not exe:
        return None
    try:
        out = subprocess.run(
            [exe, "--version"], capture_output=True, text=True, timeout=10, check=True
        ).stdout
    except (OSError, subprocess.SubprocessError):
        return None
    m = re.match(r"v(\d+)\.(\d+)", out.strip())
    return (int(m.group(1)), int(m.group(2))) if m else None


def locate(paths: Paths) -> list[str] | None:
    """The command that runs srt, or None."""
    if env := os.environ.get("CAIRNWARD_SRT"):
        return shlex.split(env)
    cli = install_dir(paths) / CLI_REL
    if cli.exists() and shutil.which("node"):
        return [str(shutil.which("node")), str(cli)]
    if exe := shutil.which("srt"):
        return [exe]
    return None


def require(paths: Paths) -> list[str]:
    if not sys.platform.startswith("linux"):
        raise SandboxUnavailable(
            "OS isolation is supported on Linux only (Windows: via WSL2, later)"
        )
    cmd = locate(paths)
    if cmd is None:
        raise SandboxUnavailable("sandbox-runtime not found: run `cairnward sandbox install`")
    return cmd


def install(paths: Paths) -> Path:
    """npm ci the pinned srt into the data folder (integrity hashes from the lockfile)."""
    version = node_version()
    if version is None or version < MIN_NODE:
        need = ".".join(map(str, MIN_NODE))
        raise SandboxUnavailable(f"Node.js >= {need} is required (found {version or 'none'})")
    npm = shutil.which("npm")
    if not npm:
        raise SandboxUnavailable("npm not found")
    target = install_dir(paths)
    target.mkdir(parents=True, exist_ok=True)
    for name in ("package.json", "package-lock.json"):
        shutil.copy(PKG_DIR / name, target / name)
    subprocess.run([npm, "ci", "--no-audit", "--no-fund", "--omit=dev"], cwd=target, check=True)
    return target / CLI_REL


@dataclass(frozen=True)
class SandboxSpec:
    allowed_hosts: list[str]
    allow_write: list[Path]
    deny_read: list[Path]
    allow_read: tuple[Path, ...] = ()  # re-opened inside denied folders
    allow_all_unix_sockets: bool = False

    def settings(self) -> dict[str, Any]:
        settings: dict[str, Any] = {
            "network": {"allowedDomains": sorted(set(self.allowed_hosts)), "deniedDomains": []},
            "filesystem": {
                "denyRead": [str(p) for p in self.deny_read if p.exists()],
                "allowRead": [str(p) for p in self.allow_read],
                "allowWrite": [str(p) for p in self.allow_write],
                "denyWrite": [],
            },
        }
        if self.allow_all_unix_sockets:
            settings["network"]["allowAllUnixSockets"] = True
        return settings

    def write(self, path: Path) -> Path:
        path.write_text(json.dumps(self.settings(), indent=2), encoding="utf-8")
        return path


def secret_dirs(home: Path | None = None) -> list[Path]:
    home = home or Path.home()
    return [home / d for d in SECRET_DIRS]


def srt_hosts(allow_hosts: list[str]) -> list[str]:
    """Allow-list entries srt accepts (it rejects catch-alls such as '*' or '*.com')."""
    out = []
    for h in allow_hosts:
        name = h.rsplit(":", 1)[0] if h.count(":") == 1 else h
        if name in {"*", ""} or (name.startswith("*.") and "." not in name[2:]):
            continue
        out.append(h)
    return out


def wrap(
    srt_cmd: list[str], settings_file: Path, command: list[str], *, tmp_dir: Path | None = None
) -> list[str]:
    """srt + clear no_proxy (+ point TMPDIR at a private folder: srt's default is a shared
    /tmp/claude that may not exist and is shared between sandboxes)."""
    inner = ["env", "-u", "no_proxy", "-u", "NO_PROXY"]
    if tmp_dir is not None:
        inner.append(f"TMPDIR={tmp_dir}")
    return [*srt_cmd, "--settings", str(settings_file), *inner, *command]


ASK_SHIM = Path(__file__).with_name("srt_ask.mjs")


def wrap_with_ask(
    srt_cmd: list[str],
    settings_file: Path,
    command: list[str],
    *,
    ask_socket: Path,
    run_id: str,
    tmp_dir: Path | None = None,
) -> list[str] | None:
    """Like `wrap`, but hosts not on the allow list are referred to Cairnward (srt's ask
    callback, through a small Node wrapper around srt's library). Needs srt as
    `node .../dist/cli.js`; returns None otherwise (the caller falls back to `wrap`)."""
    if len(srt_cmd) != 2 or Path(srt_cmd[1]).name != "cli.js":
        return None
    pkg_dir = Path(srt_cmd[1]).parent.parent
    if not (pkg_dir / "dist" / "index.js").exists():
        return None
    inner = wrap([], settings_file, command, tmp_dir=tmp_dir)[2:]  # drop "--settings <file>"
    return [srt_cmd[0], str(ASK_SHIM), str(pkg_dir), str(settings_file), str(ask_socket),
            run_id, "--", *inner]  # fmt: skip
