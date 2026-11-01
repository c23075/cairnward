"""Artifacts: a git repository per conversation, research job or agent run (spec §4.7,
FR-ART-1). Every write is one commit; restoring an old version is a new commit, so
history is never rewritten. Repositories live in `<data>/artifacts/<scope>/<id>/`
(owner-only) and are deleted with their conversation when it is purged.

Agent runs already get workspace snapshots (`cairnward runs`); this store holds what
models produce in conversations (through the MCP tool) and research job results.
"""

from __future__ import annotations

import os
import re
import shutil
import subprocess
from dataclasses import dataclass
from pathlib import Path, PurePosixPath
from typing import Literal

Scope = Literal["conversation", "job", "run"]
SCOPES: tuple[Scope, ...] = ("conversation", "job", "run")
MAX_BYTES = 2 * 1024 * 1024
_ID = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,80}$")


class ArtifactError(Exception):
    pass


@dataclass(frozen=True)
class Version:
    rev: str
    date: str
    message: str


def clean_path(path: str) -> str:
    p = PurePosixPath(path.replace("\\", "/").strip())
    if p.is_absolute() or not p.parts or any(part in ("..", ".", "") for part in p.parts):
        raise ArtifactError(f"'{path}' must be a relative path inside the artifacts folder")
    if p.parts[0] == ".git" or any(part.startswith(".git") for part in p.parts):
        raise ArtifactError("paths under .git are not allowed")
    return str(p)


class ArtifactStore:
    def __init__(self, data_dir: Path):
        self.root = data_dir / "artifacts"

    def repo(self, scope: Scope, owner: str) -> Path:
        if scope not in SCOPES or not _ID.match(owner):
            raise ArtifactError(f"bad artifact repository {scope}/{owner}")
        return self.root / scope / owner

    def _git(self, repo: Path, *args: str, input_bytes: bytes | None = None,
             check: bool = True) -> bytes:  # fmt: skip
        git = shutil.which("git")
        if git is None:
            raise ArtifactError("git is not installed (needed for artifacts)")
        env = {"PATH": os.environ.get("PATH", ""), "HOME": os.environ.get("HOME", ""),
               "GIT_AUTHOR_NAME": "cairnward", "GIT_AUTHOR_EMAIL": "cairnward@localhost",
               "GIT_COMMITTER_NAME": "cairnward", "GIT_COMMITTER_EMAIL": "cairnward@localhost",
               "GIT_CONFIG_NOSYSTEM": "1", "GIT_CONFIG_GLOBAL": os.devnull,
               "GIT_TERMINAL_PROMPT": "0"}  # fmt: skip
        res = subprocess.run([git, *args], cwd=repo, env=env, input=input_bytes,
                             capture_output=True, check=False)  # fmt: skip
        if check and res.returncode != 0:
            raise ArtifactError(f"git {args[0]}: {res.stderr.decode('utf-8', 'replace')[:300]}")
        return res.stdout

    def _ensure(self, scope: Scope, owner: str) -> Path:
        repo = self.repo(scope, owner)
        if not (repo / ".git").exists():
            repo.mkdir(parents=True, exist_ok=True)
            for d in (self.root, self.root / scope, repo):
                if os.name != "nt":
                    d.chmod(0o700)
            self._git(repo, "init", "--quiet", "-b", "main")
            self._git(repo, "config", "core.autocrlf", "false")
        return repo

    def write(self, scope: Scope, owner: str, path: str, content: bytes | str,
              message: str | None = None) -> str:  # fmt: skip
        """Write one file and commit it; returns the new revision."""
        rel = clean_path(path)
        data = content.encode() if isinstance(content, str) else content
        if len(data) > MAX_BYTES:
            raise ArtifactError(f"artifacts are limited to {MAX_BYTES // 1024 // 1024} MB each")
        repo = self._ensure(scope, owner)
        target = repo / rel
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(data)
        self._git(repo, "add", "--", rel)
        if not self._git(repo, "status", "--porcelain", "--", rel).strip():
            return self._git(repo, "rev-parse", "HEAD").decode().strip()  # unchanged
        self._git(repo, "commit", "--quiet", "-m", message or f"write {rel}")
        return self._git(repo, "rev-parse", "HEAD").decode().strip()

    def files(self, scope: Scope, owner: str) -> list[str]:
        repo = self.repo(scope, owner)
        if not (repo / ".git").exists():
            return []
        out = self._git(repo, "ls-files", "-z").decode()
        return sorted(p for p in out.split("\0") if p)

    def read(self, scope: Scope, owner: str, path: str, rev: str | None = None) -> bytes:
        rel = clean_path(path)
        repo = self.repo(scope, owner)
        if rev is not None and not re.fullmatch(r"[0-9a-f]{7,40}", rev):
            raise ArtifactError("rev must be a commit id")
        try:
            return self._git(repo, "show", f"{rev or 'HEAD'}:{rel}")
        except (ArtifactError, FileNotFoundError, NotADirectoryError):
            raise ArtifactError(f"no artifact '{rel}'" + (f" at {rev}" if rev else "")) from None

    def log(self, scope: Scope, owner: str, path: str | None = None) -> list[Version]:
        repo = self.repo(scope, owner)
        if not (repo / ".git").exists():
            return []
        args = ["log", "--format=%H%x1f%cI%x1f%s"]
        if path:
            args += ["--", clean_path(path)]
        out = self._git(repo, *args, check=False).decode()
        return [Version(*line.split("\x1f", 2)) for line in out.splitlines() if line]

    def restore(self, scope: Scope, owner: str, path: str, rev: str) -> str:
        """Bring back an old version as a new commit (history is kept)."""
        old = self.read(scope, owner, path, rev)
        return self.write(scope, owner, path, old, f"restore {clean_path(path)} to {rev[:12]}")

    def repos(self) -> list[tuple[Scope, str, int]]:
        out: list[tuple[Scope, str, int]] = []
        for scope in SCOPES:
            base = self.root / scope
            if base.is_dir():
                out += [(scope, d.name, len(self.files(scope, d.name)))
                        for d in sorted(base.iterdir()) if (d / ".git").exists()]  # fmt: skip
        return out

    def purge(self, scope: Scope, owner: str) -> bool:
        repo = self.repo(scope, owner)
        if not repo.exists():
            return False
        shutil.rmtree(repo)
        return True
