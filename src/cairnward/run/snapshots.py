"""Workspace snapshots around each `cairnward run` (spec §4.3, FR-AGT-2).

Before and after a run, the workspace is committed into a *shadow* git repository in
Cairnward's state folder (one per workspace), so every change an agent made can be
reviewed (`cairnward runs diff`) and undone (`cairnward runs revert`). The project's own
`.git` is never touched, and folders that aren't git repositories work too. The
workspace's `.gitignore` files are honoured; common build and dependency folders are
excluded as well.

Privacy: snapshots copy workspace files into the state folder (owner-only).
`cairnward runs prune` removes old ones.
"""

from __future__ import annotations

import hashlib
import os
import shutil
import subprocess
from dataclasses import dataclass
from pathlib import Path

DEFAULT_EXCLUDES = [
    "node_modules/", ".venv/", "venv/", "__pycache__/", ".mypy_cache/", ".pytest_cache/",
    ".ruff_cache/", "dist/", "build/", "target/", ".tox/", "*.pyc", ".DS_Store",
    ".aider.tags.cache.v*/",
]  # fmt: skip
MAX_FILE_BYTES = 20 * 1024 * 1024  # larger files are left out of snapshots


class SnapshotError(Exception):
    pass


@dataclass(frozen=True)
class Change:
    status: str  # A added, M modified, D deleted (R/C reported as M by --no-renames)
    path: str


@dataclass(frozen=True)
class RevertResult:
    restored: list[str]
    conflicts: list[str]  # changed again since the run; left alone unless forced


class Snapshots:
    def __init__(self, state_dir: Path, workspace: Path):
        self.workspace = workspace.resolve()
        key = hashlib.sha256(str(self.workspace).encode()).hexdigest()[:16]
        self.git_dir = state_dir / "snapshots" / f"{key}.git"

    # ------------------------------------------------------------------ git plumbing

    def _git(self, *args: str, index: Path | None = None, check: bool = True) -> str:
        return self._run(args, index=index, check=check).decode("utf-8", "replace")

    def _run(
        self,
        args: tuple[str, ...],
        *,
        index: Path | None = None,
        check: bool = True,
        repo_env: bool = True,
    ) -> bytes:
        git = shutil.which("git")
        if git is None:
            raise SnapshotError("git is not installed (needed for run snapshots)")
        env = {
            "PATH": os.environ.get("PATH", ""),
            "HOME": os.environ.get("HOME", ""),
            "GIT_AUTHOR_NAME": "cairnward", "GIT_AUTHOR_EMAIL": "cairnward@localhost",
            "GIT_COMMITTER_NAME": "cairnward", "GIT_COMMITTER_EMAIL": "cairnward@localhost",
            "GIT_CONFIG_NOSYSTEM": "1", "GIT_CONFIG_GLOBAL": os.devnull,
            "GIT_TERMINAL_PROMPT": "0",
        }  # fmt: skip
        if repo_env:
            env.update(GIT_DIR=str(self.git_dir), GIT_WORK_TREE=str(self.workspace))
        if index is not None:
            env["GIT_INDEX_FILE"] = str(index)
        res = subprocess.run([git, *args], cwd=self.workspace, env=env, capture_output=True,
                             check=False)  # fmt: skip
        if check and res.returncode != 0:
            err = res.stderr.decode("utf-8", "replace").strip()[:300]
            raise SnapshotError(f"git {args[0]} failed: {err}")
        return res.stdout

    def _init(self) -> None:
        if (self.git_dir / "HEAD").exists():
            return
        self.git_dir.parent.mkdir(parents=True, exist_ok=True)
        self.git_dir.parent.chmod(0o700)
        self._run(("init", "--quiet", "--bare", str(self.git_dir)), repo_env=False)
        self._git("config", "core.bare", "false")
        self._git("config", "core.autocrlf", "false")
        self._git("config", "gc.auto", "0")
        (self.git_dir / "info").mkdir(exist_ok=True)
        (self.git_dir / "info" / "exclude").write_text("\n".join(DEFAULT_EXCLUDES) + "\n")
        (self.git_dir / "cairnward-workspace").write_text(str(self.workspace) + "\n")

    @staticmethod
    def ref(run_id: str, which: str) -> str:
        return f"refs/cairnward/runs/{run_id}/{which}"

    # ------------------------------------------------------------------ snapshots

    def take(self, run_id: str, which: str) -> str:
        """Commit the workspace as it is now; returns the commit id."""
        self._init()
        index = self.git_dir / f"index-{run_id}-{which}"
        try:
            self._git("add", "--all", ".", index=index)
            # leave out very large files (logs, datasets, binaries)
            listed = self._git("ls-files", "-z", "--cached", index=index).split("\0")
            for rel in filter(None, listed):
                try:
                    if (self.workspace / rel).stat().st_size > MAX_FILE_BYTES:
                        self._git("rm", "--cached", "--quiet", "--", rel, index=index)
                except OSError:
                    continue
            tree = self._git("write-tree", index=index).strip()
        finally:
            index.unlink(missing_ok=True)
        commit = self._git("commit-tree", tree, "-m", f"cairnward run {run_id} {which}").strip()
        self._git("update-ref", self.ref(run_id, which), commit)
        return commit

    def changes(self, run_id: str) -> list[Change]:
        out = self._git("diff", "--name-status", "--no-renames", "-z",
                        self.ref(run_id, "before"), self.ref(run_id, "after"))  # fmt: skip
        parts = [p for p in out.split("\0") if p]
        return [Change(parts[i], parts[i + 1]) for i in range(0, len(parts) - 1, 2)]

    def diff(self, run_id: str, *, stat: bool = False) -> str:
        args = ["diff", "--no-renames", "--no-color"]
        if stat:
            args.append("--stat")
        return self._git(*args, self.ref(run_id, "before"), self.ref(run_id, "after"))

    def has_run(self, run_id: str) -> bool:
        if not (self.git_dir / "HEAD").exists():
            return False
        out = self._git("rev-parse", "--verify", "--quiet", self.ref(run_id, "after"), check=False)
        return bool(out.strip())

    def _blob(self, ref: str, path: str) -> str | None:
        out = self._git("rev-parse", "--verify", "--quiet", f"{ref}:{path}", check=False).strip()
        return out or None

    def _current_blob(self, path: str) -> str | None:
        p = self.workspace / path
        if not p.is_file():
            return None
        return self._git("hash-object", "--", path).strip()

    def revert(self, run_id: str, *, force: bool = False) -> RevertResult:
        """Undo the run's changes. Files changed again since the run are conflicts and are
        left alone unless `force`. A safety snapshot is taken first."""
        self.take(run_id, "pre-revert")
        before, after = self.ref(run_id, "before"), self.ref(run_id, "after")
        restored, conflicts = [], []
        for change in self.changes(run_id):
            path = change.path
            if not force and self._current_blob(path) != self._blob(after, path):
                conflicts.append(path)
                continue
            target = self.workspace / path
            if change.status == "A":
                target.unlink(missing_ok=True)
            else:
                target.parent.mkdir(parents=True, exist_ok=True)
                target.write_bytes(self._run(("cat-file", "blob", f"{before}:{path}")))
            restored.append(path)
        return RevertResult(restored, conflicts)

    def runs(self) -> list[str]:
        if not (self.git_dir / "HEAD").exists():
            return []
        out = self._git("for-each-ref", "--format=%(refname)", "refs/cairnward/runs/")
        ids = {line.split("/")[3] for line in out.splitlines() if line.count("/") >= 4}
        return sorted(ids)

    def prune(self, keep_run_ids: set[str]) -> int:
        """Delete snapshots of runs not in `keep_run_ids`, then drop unreachable objects."""
        removed = 0
        for run_id in self.runs():
            if run_id in keep_run_ids:
                continue
            for which in ("before", "after", "pre-revert"):
                self._git("update-ref", "-d", self.ref(run_id, which), check=False)
            removed += 1
        if removed:
            self._git("reflog", "expire", "--expire=now", "--all", check=False)
            self._git("gc", "--prune=now", "--quiet", check=False)
        return removed
