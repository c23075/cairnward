"""OpenShell as a second sandbox for `cairnward run` (ADR-0018, OpenShell review 2026-11).

With `sandbox = "openshell"` in an environment (and isolation on), a run's agent starts in an
OpenShell sandbox instead of srt. OpenShell enforces the network side: the sandbox's policy
allows exactly two destinations, Cairnward's gateway and cairnward-egress on this machine (as
`host.openshell.internal`), as plain TCP. Everything else is refused by OpenShell's supervisor,
for every program and protocol, not only those that honour proxy variables. Cairnward stays
the policy authority: model and tool calls go through its gateway, and other traffic through
egress, which applies the environment's network rules, approvals and audit as for srt.

A sandbox has its own file system, so the run works on a copy:
1. `sandbox create --detach` with the policy, then `exec -- pwd` finds its working folder;
2. the run's settings (an env file with the run key, profile config, CA files for TLS
   inspection) are uploaded as `.cairnward-run`, and the workspace beside it;
3. the agent runs with `sandbox exec`, which sources the env file first, so the run key never
   appears in a command line on this machine;
4. the workspace is downloaded and mirrored back: new and changed files are written, files the
   agent deleted are deleted. Snapshots before and after still give `runs diff` and `revert`;
5. the sandbox is deleted. If the download fails, nothing in the workspace is touched and the
   sandbox is kept, so the work can be fetched by hand.

Written against OpenShell 0.1.x's documented CLI. Tested here against a stand-in CLI
(`tests/fakes/openshell_cli.py`), not a real OpenShell gateway: see docs/status.md.
"""

from __future__ import annotations

import filecmp
import json
import os
import shlex
import shutil
import subprocess
import sys
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path

RUN_DIR = ".cairnward-run"
ENV_FILE = "env.sh"
# Variables from this machine that mean nothing inside an image (its own PATH, HOME, user).
HOST_ONLY = {"PATH", "HOME", "USER", "LOGNAME", "SHELL", "VIRTUAL_ENV", "TMPDIR"}


class OpenShellError(Exception):
    pass


def locate() -> list[str] | None:
    """The openshell command: $CAIRNWARD_OPENSHELL (may include an interpreter), else PATH."""
    custom = os.environ.get("CAIRNWARD_OPENSHELL")
    if custom:
        return shlex.split(custom)
    found = shutil.which("openshell")
    return [found] if found else None


def policy(host_name: str, ports: list[int]) -> dict[str, object]:
    """The sandbox policy: Cairnward's ports on this machine, nothing else.

    `protocol: tcp` passes the bytes unread: the gateway call carries the run key, and egress
    does its own checks. `binaries: /**` lets every program in the image use the rule, as srt
    lets every program use its proxy. That a whole-tree glob is accepted is from the schema's
    description ("path or glob"), not from a run against a real gateway."""
    return {
        "version": 1,
        "filesystem_policy": {
            "include_workdir": True,
            "read_only": ["/"],
            "read_write": ["/sandbox", "/tmp"],  # noqa: S108 (inside the sandbox)
        },
        "landlock": {"compatibility": "hard_requirement"},
        "network_policies": {
            "cairnward": {
                "name": "Cairnward gateway and egress",
                "endpoints": [{"host": host_name, "port": p, "protocol": "tcp"} for p in ports],
                "binaries": [{"path": "/**"}],
            }
        },
    }


def env_script(env: dict[str, str]) -> str:
    lines = ["# written by cairnward run; removed with the sandbox"]
    lines += [f"export {k}={shlex.quote(v)}" for k, v in sorted(env.items()) if k.isidentifier()]
    return "\n".join(lines) + "\n"


def manifest(root: Path) -> dict[str, str]:
    """Relative path -> 'f' (file) or 'l' (symlink), for everything under root but folders."""
    out: dict[str, str] = {}
    for dirpath, dirnames, filenames in os.walk(root):
        base = Path(dirpath)
        for name in [*filenames, *(d for d in dirnames if (base / d).is_symlink())]:
            p = base / name
            out[str(p.relative_to(root))] = "l" if p.is_symlink() else "f"
    return out


def _through_link(workspace: Path, rel: str) -> bool:
    """Whether a folder on the way to `rel` is a symlink, so writing or deleting there could
    reach outside the workspace (srt would refuse such a write)."""
    p = workspace
    for part in Path(rel).parts[:-1]:
        p = p / part
        if p.is_symlink():
            return True
    return False


def mirror_back(copy: Path, workspace: Path, before: dict[str, str]) -> tuple[int, int, list[str]]:
    """Make the workspace match the downloaded copy: write new and changed files, delete files
    that were there before the run and are gone from the copy. Nothing is written or deleted
    through a symlinked folder, and a folder isn't replaced by a symlink: those paths are
    returned as refused. Returns (written, deleted, refused)."""
    after = manifest(copy)
    written = deleted = 0
    refused: list[str] = []
    for rel, kind in sorted(after.items()):
        src, dst = copy / rel, workspace / rel
        if _through_link(workspace, rel) or (dst.is_dir() and not dst.is_symlink()):
            refused.append(rel)
            continue
        if kind == "l":
            target = src.readlink()
            if dst.is_symlink() and dst.readlink() == target:
                continue
            if dst.is_symlink() or dst.is_file():
                dst.unlink()
            dst.parent.mkdir(parents=True, exist_ok=True)
            dst.symlink_to(target)
            written += 1
            continue
        if dst.is_file() and not dst.is_symlink() and filecmp.cmp(src, dst, shallow=False):
            if (src.stat().st_mode & 0o777) != (dst.stat().st_mode & 0o777):
                dst.chmod(src.stat().st_mode & 0o777)
            continue
        if dst.is_symlink():
            dst.unlink()
        dst.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(src, dst)
        written += 1
    kept = {Path(r) for r in refused}
    for rel in sorted(before.keys() - after.keys()):
        p = workspace / rel
        if _through_link(workspace, rel) or kept.intersection(Path(rel).parents):
            refused.append(rel)
        elif p.is_symlink() or p.is_file():
            p.unlink()
            deleted += 1
    return written, deleted, refused


Runner = Callable[..., subprocess.CompletedProcess[str]]


def _run(argv: list[str], **kw: object) -> subprocess.CompletedProcess[str]:
    return subprocess.run(argv, capture_output=True, text=True, check=False, timeout=600, **kw)  # type: ignore[call-overload,no-any-return]  # noqa: S603


@dataclass
class OpenShellRun:
    """One run's sandbox. Use `prepare`, then `agent_argv` to start the agent, then `finish`."""

    cli: list[str]
    name: str
    workspace: Path
    stage: Path  # a private local folder named RUN_DIR, uploaded beside the workspace
    image: str | None = None
    run: Runner = _run
    workdir: str = ""
    policy_file: Path = Path()
    created: bool = False
    before: dict[str, str] = field(default_factory=dict)

    def _cli(self, *args: str) -> subprocess.CompletedProcess[str]:
        argv = [*self.cli, *args]
        res = self.run(argv)
        if res.returncode != 0:
            what = " ".join(args[:2])
            raise OpenShellError(f"openshell {what} failed: {(res.stderr or res.stdout).strip()}")
        return res

    def create(self, policy_file: Path) -> None:
        args = ["sandbox", "create", "--name", self.name, "--policy", str(policy_file),
                "--detach"]  # fmt: skip
        if self.image:
            args += ["--from", self.image]
        self._cli(*args)
        self.created = True
        out = self._cli("sandbox", "exec", "-n", self.name, "--no-login-shell", "--no-tty",
                        "--", "pwd").stdout.strip().splitlines()  # fmt: skip
        if not out or not out[-1].startswith("/"):
            raise OpenShellError(f"couldn't find the sandbox's working folder (pwd said {out!r})")
        self.workdir = out[-1].rstrip("/") or "/"

    def sandbox_path(self, *parts: str) -> str:
        return "/".join([self.workdir.rstrip("/"), *parts])

    def upload(self, env: dict[str, str]) -> None:
        """Write the env file (paths under the stage pointed at its sandbox copy), then upload
        the stage and the workspace."""
        plan_files(self.stage, env, self.stage, self.sandbox_path(RUN_DIR))
        self.before = manifest(self.workspace)
        self._cli("sandbox", "upload", self.name, str(self.stage), "--no-git-ignore")
        self._cli("sandbox", "upload", self.name, str(self.workspace), "--no-git-ignore")

    def agent_argv(self, command: list[str], *, tty: bool) -> list[str]:
        env_file = self.sandbox_path(RUN_DIR, ENV_FILE)
        return [*self.cli, "sandbox", "exec", "-n", self.name, "--no-login-shell",
                "--tty" if tty else "--no-tty",
                "--workdir", self.sandbox_path(self.workspace.name),
                "--", "sh", "-c", '. "$0" && exec "$@"', env_file, *command]  # fmt: skip

    def download(self, into: Path) -> Path:
        """Fetch the workspace copy into a new folder under `into`; returns its root."""
        dest = into / "back"
        self._cli("sandbox", "download", self.name, self.workspace.name, str(dest))
        nested = dest / self.workspace.name
        if (
            dest.is_dir()
            and nested.is_dir()
            and [p.name for p in dest.iterdir()] == [self.workspace.name]
            and not (self.workspace / self.workspace.name).exists()
        ):
            return nested  # cp-style: the folder landed inside dest
        if not dest.is_dir():
            raise OpenShellError("the download produced no folder")
        return dest

    def delete(self) -> None:
        if self.created:
            self.run([*self.cli, "sandbox", "delete", self.name])
            self.created = False


def plan_files(stage: Path, env: dict[str, str], host_stage: Path, sandbox_stage: str) -> None:
    """Write the env file, pointing paths under the local stage at its sandbox copy."""
    fixed = {k: v.replace(str(host_stage), sandbox_stage) for k, v in env.items()}
    f = stage / ENV_FILE
    f.write_text(env_script(fixed), encoding="utf-8")
    f.chmod(0o600)


def write_policy(path: Path, host_name: str, ports: list[int]) -> Path:
    # YAML 1.2 accepts JSON, so no YAML library is needed to write one.
    path.write_text(json.dumps(policy(host_name, ports), indent=2) + "\n", encoding="utf-8")
    return path


def stdin_is_tty() -> bool:
    try:
        return sys.stdin.isatty() and sys.stdout.isatty()
    except (AttributeError, ValueError):
        return False
