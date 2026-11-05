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
from cairnward.sysinfo import windows_mounts, wsl_version

SRT_VERSION = "0.0.81"
MIN_NODE = (22, 12)
MIN_NODE_TEXT = ".".join(map(str, MIN_NODE))
PKG_DIR = Path(__file__).parent / "srt_pkg"
CLI_REL = Path("node_modules/@anthropic-ai/sandbox-runtime/dist/cli.js")

# Never readable from inside a sandbox (relative to the home directory). Runs hide the whole
# home folder by default; this list still applies inside what they re-allow, and it's all the
# core, MCP servers and plugins have.
SECRET_DIRS = (
    # keys and cloud or code-host credentials
    ".ssh",
    ".gnupg",
    ".aws",
    ".azure",
    ".oci",
    ".config/gcloud",
    ".config/doctl",
    ".kube",
    ".docker",
    ".password-store",
    ".local/share/keyrings",
    ".netrc",
    ".git-credentials",
    ".config/git/credentials",
    ".config/gh",
    ".config/hub",
    ".config/glab-cli",
    ".config/rclone",
    ".config/restic",
    ".vault-token",
    ".terraform.d/credentials.tfrc.json",
    ".s3cfg",
    ".boto",
    ".pgpass",
    ".my.cnf",
    ".env",
    # package registries
    ".pypirc",
    ".npmrc",
    ".yarnrc.yml",
    ".config/pip",
    ".pip",
    ".config/uv/uv.toml",
    ".cargo/credentials",
    ".cargo/credentials.toml",
    ".gem/credentials",
    ".m2/settings.xml",
    ".gradle/gradle.properties",
    # other AI tools' sign-ins (agent state folders stay readable to their own runs)
    ".claude",
    ".claude.json",
    ".codex",
    ".aider/oauth-keys.env",
    ".qwen/oauth_creds.json",
    # shell and tool history
    ".bash_history",
    ".zsh_history",
    ".zhistory",
    ".histfile",
    ".local/share/fish",
    ".python_history",
    ".node_repl_history",
    ".psql_history",
    ".mysql_history",
    ".sqlite_history",
    ".rediscli_history",
    ".lesshst",
    ".viminfo",
    # browsers and their caches
    ".mozilla",
    ".librewolf",
    ".waterfox",
    ".floorp",
    ".zen",
    ".config/google-chrome",
    ".config/chromium",
    ".config/BraveSoftware",
    ".config/microsoft-edge",
    ".config/vivaldi",
    ".config/opera",
    ".cache/mozilla",
    ".cache/librewolf",
    ".cache/google-chrome",
    ".cache/chromium",
    ".cache/BraveSoftware",
    ".cache/microsoft-edge",
    ".cache/vivaldi",
    ".cache/opera",
    # mail, messaging and editors (saved sign-ins, open files, message stores)
    ".thunderbird",
    ".cache/thunderbird",
    ".config/Signal",
    ".config/Slack",
    ".config/discord",
    ".config/Element",
    ".local/share/TelegramDesktop",
    ".config/Code",
    ".config/Code - OSS",
    ".config/VSCodium",
    ".config/Cursor",
    ".vscode",
    ".vscode-server",
    # flatpak and snap apps keep all of the above in their own folders
    ".var/app",
    "snap",
)


# Under WSL: the way out to Windows. Starting a Windows program runs /init, which asks Windows
# over the socket in /run/WSL; that program is outside the sandbox and its network rules.
WSL_HIDDEN = ("/init", "/run/WSL", "/mnt/wsl", "/mnt/wslg")


class SandboxUnavailable(Exception):
    pass


def windows_paths() -> list[Path]:
    """What no sandbox may read under WSL: the Windows drives and the way to Windows programs.
    Empty elsewhere. A workspace on a drive stays reachable: srt re-binds write paths."""
    if wsl_version() is None:
        return []
    return [*map(Path, WSL_HIDDEN), *windows_mounts()]


def install_dir(paths: Paths) -> Path:
    return paths.data_dir / "sandbox-runtime"


def srt_dirs(paths: Paths, srt_cmd: list[str]) -> list[Path]:
    """What must stay readable for srt's helpers (seccomp) to start inside the sandbox: the
    install folder, and the package `srt_cmd` runs from ($CAIRNWARD_SRT, `srt` on PATH)."""
    dirs = [install_dir(paths)]
    cli = Path(shutil.which(srt_cmd[-1]) or srt_cmd[-1]).resolve()
    if cli.name == CLI_REL.name:
        dirs.append(cli.parent.parent)
    return dirs


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
            "OS isolation is supported on Linux only (on Windows, run Cairnward inside WSL2)"
        )
    if wsl_version() == 1:
        raise SandboxUnavailable(
            "OS isolation needs WSL2; this is WSL1 (in Windows: wsl --set-version <distro> 2)"
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
        deny = dict.fromkeys([*self.deny_read, *windows_paths()])  # here so no sandbox misses it
        settings: dict[str, Any] = {
            "network": {"allowedDomains": sorted(set(self.allowed_hosts)), "deniedDomains": []},
            "filesystem": {
                "denyRead": [str(p) for p in deny if p.exists()],
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
