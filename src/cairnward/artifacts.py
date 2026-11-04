"""Artifacts: a git repository per conversation, research job or agent run (spec §4.7,
FR-ART-1). Every write is one commit; restoring an old version is a new commit, so
history is never rewritten. Repositories live in `<data>/artifacts/<scope>/<id>/`
(owner-only) and are deleted with their conversation when it is purged.

Locking (FR-ENC-1): a locked conversation's whole repository, history and file names
included, is one file (`sealed.age`) encrypted with the conversation's own age key, so its
passphrase, recovery key and crypto-shredding all apply. While the conversation is unlocked,
each operation works on a temporary copy, which a write seals again. Plaintext is deleted
normally: copies already in backups or left on the disk aren't reached by locking.

A finished research job or a run's repository can be locked too, with a key of its own. A
job's folder (its notes and result) is sealed with it, leaving a stub that says only that the
job exists. These stay closed until the lock is removed: nothing writes to them afterwards,
so there's no temporary unlocking. Run snapshots (`cairnward runs`) are kept per workspace,
not per run, and aren't covered.

Agent runs already get workspace snapshots (`cairnward runs`); this store holds what
models produce in conversations (through the MCP tool) and research job results.
"""

from __future__ import annotations

import io
import json
import os
import re
import shutil
import subprocess
import tarfile
import tempfile
import threading
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path, PurePosixPath
from typing import Any, Literal, Protocol

from cairnward.store import crypto

Scope = Literal["conversation", "job", "run"]
SCOPES: tuple[Scope, ...] = ("conversation", "job", "run")
MAX_BYTES = 2 * 1024 * 1024
_ID = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,80}$")
SEALED = "sealed.age"
LOCKED_FILES = -1  # in repos(): the file count of a locked repository isn't known
_seal_lock = threading.Lock()  # a sealed repository is rewritten whole on every write


class ArtifactError(Exception):
    pass


class ArtifactLockedError(ArtifactError):
    pass


class ConversationKeys(Protocol):
    """What the store needs from the vault to open a locked conversation's repository."""

    def is_locked(self, conversation_id: str) -> bool: ...

    def artifact_key(self, conversation_id: str) -> bytes | None: ...


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
    def __init__(self, data_dir: Path, keys: ConversationKeys | None = None):
        self.root = data_dir / "artifacts"
        self.keys = keys

    def repo(self, scope: Scope, owner: str) -> Path:
        if scope not in SCOPES or not _ID.match(owner):
            raise ArtifactError(f"bad artifact repository {scope}/{owner}")
        return self.root / scope / owner

    def _git(self, repo: Path, *args: str, input_bytes: bytes | None = None,
             check: bool = True, extra_env: dict[str, str] | None = None) -> bytes:  # fmt: skip
        git = shutil.which("git")
        if git is None:
            raise ArtifactError("git is not installed (needed for artifacts)")
        env = {"PATH": os.environ.get("PATH", ""), "HOME": os.environ.get("HOME", ""),
               "GIT_AUTHOR_NAME": "cairnward", "GIT_AUTHOR_EMAIL": "cairnward@localhost",
               "GIT_COMMITTER_NAME": "cairnward", "GIT_COMMITTER_EMAIL": "cairnward@localhost",
               "GIT_CONFIG_NOSYSTEM": "1", "GIT_CONFIG_GLOBAL": os.devnull,
               "GIT_TERMINAL_PROMPT": "0", **(extra_env or {})}  # fmt: skip
        res = subprocess.run([git, *args], cwd=repo, env=env, input=input_bytes,
                             capture_output=True, check=False)  # fmt: skip
        if check and res.returncode != 0:
            raise ArtifactError(f"git {args[0]}: {res.stderr.decode('utf-8', 'replace')[:300]}")
        return res.stdout

    def _ensure(self, repo: Path) -> None:
        if not (repo / ".git").exists():
            repo.mkdir(parents=True, exist_ok=True)
            if os.name != "nt":
                repo.chmod(0o700)
            self._git(repo, "init", "--quiet", "-b", "main")
            self._git(repo, "config", "core.autocrlf", "false")

    def _create_parents(self, scope: Scope) -> None:
        (self.root / scope).mkdir(parents=True, exist_ok=True)
        if os.name != "nt":
            for d in (self.root, self.root / scope):
                d.chmod(0o700)

    # ------------------------------------------------------------------ locking

    def seal(self, owner: str, key: bytes) -> None:
        """Encrypt a conversation's repository in place, if it has plaintext to encrypt."""
        repo = self.repo("conversation", owner)
        with _seal_lock:
            if (repo / ".git").exists() and not (repo / SEALED).exists():
                _write_atomic(repo / SEALED, crypto.seal(key, _pack(repo)))
            _remove_plaintext(repo)

    def unseal(self, owner: str, key: bytes) -> None:
        """Turn a sealed repository back into a plain one (the lock was removed)."""
        repo = self.repo("conversation", owner)
        with _seal_lock:
            if (repo / SEALED).exists():
                _remove_plaintext(repo)
                _unpack(self._open(repo, key), repo)
                (repo / SEALED).unlink()

    @staticmethod
    def _open(repo: Path, key: bytes) -> bytes:
        try:
            return crypto.open_sealed(key, (repo / SEALED).read_bytes())
        except crypto.DecryptionError as exc:
            raise ArtifactLockedError(f"can't decrypt {repo.name}'s artifacts") from exc

    def _key(self, owner: str) -> bytes:
        key = self.keys.artifact_key(owner) if self.keys is not None else None
        if key is None:
            raise ArtifactLockedError(
                f"the artifacts of conversation {owner} are locked: unlock it first "
                "(cairnward vault unlock)"
            )
        return key

    @contextmanager
    def _working(self, scope: Scope, owner: str, *, write: bool = False) -> Iterator[Path]:
        """The repository to work on: the real one, or for a locked conversation a
        temporary copy that a write seals again."""
        repo = self.repo(scope, owner)
        if scope != "conversation":
            if (repo / SEALED).exists():
                raise ArtifactLockedError(
                    f"this {scope}'s artifacts are locked: remove the lock first "
                    f"(cairnward artifacts unlock {scope} {owner})"
                )
            yield repo
            return
        sealed = (repo / SEALED).exists()
        if not sealed and not (self.keys is not None and self.keys.is_locked(owner)):
            yield repo
            return
        key = self._key(owner)
        with _seal_lock:
            if sealed:
                _remove_plaintext(repo)  # left by a crash while sealing; the bundle is whole
            tmp = Path(tempfile.mkdtemp(prefix="cw-art-", dir=_private_tmp()))
            try:
                copy = tmp / "repo"
                copy.mkdir()
                if sealed:
                    _unpack(self._open(repo, key), copy)
                elif (repo / ".git").exists():  # locked before artifacts could be
                    shutil.copytree(repo, copy, dirs_exist_ok=True)
                yield copy
                if write:
                    repo.mkdir(parents=True, exist_ok=True)
                    if os.name != "nt":
                        repo.chmod(0o700)
                    _write_atomic(repo / SEALED, crypto.seal(key, _pack(copy)))
                    _remove_plaintext(repo)
            finally:
                remove_tree(tmp)

    # ------------------------------------------------------------------ files

    def write(self, scope: Scope, owner: str, path: str, content: bytes | str,
              message: str | None = None) -> str:  # fmt: skip
        """Write one file and commit it; returns the new revision."""
        rel = clean_path(path)
        data = content.encode() if isinstance(content, str) else content
        if len(data) > MAX_BYTES:
            raise ArtifactError(f"artifacts are limited to {MAX_BYTES // 1024 // 1024} MB each")
        self._create_parents(scope)
        with self._working(scope, owner, write=True) as repo:
            self._ensure(repo)
            target = repo / rel
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_bytes(data)
            self._git(repo, "add", "--", rel)
            if not self._git(repo, "status", "--porcelain", "--", rel).strip():
                return self._git(repo, "rev-parse", "HEAD").decode().strip()  # unchanged
            self._git(repo, "commit", "--quiet", "-m", message or f"write {rel}")
            return self._git(repo, "rev-parse", "HEAD").decode().strip()

    def files(self, scope: Scope, owner: str) -> list[str]:
        with self._working(scope, owner) as repo:
            if not (repo / ".git").exists():
                return []
            out = self._git(repo, "ls-files", "-z").decode()
            return sorted(p for p in out.split("\0") if p)

    def read(self, scope: Scope, owner: str, path: str, rev: str | None = None) -> bytes:
        rel = clean_path(path)
        if rev is not None and not re.fullmatch(r"[0-9a-f]{7,40}", rev):
            raise ArtifactError("rev must be a commit id")
        with self._working(scope, owner) as repo:
            try:
                return self._git(repo, "show", f"{rev or 'HEAD'}:{rel}")
            except (ArtifactError, FileNotFoundError, NotADirectoryError):
                where = f" at {rev}" if rev else ""
                raise ArtifactError(f"no artifact '{rel}'{where}") from None

    def log(self, scope: Scope, owner: str, path: str | None = None) -> list[Version]:
        with self._working(scope, owner) as repo:
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
        """Every repository and its number of files (LOCKED_FILES if locked)."""
        out: list[tuple[Scope, str, int]] = []
        for scope in SCOPES:
            base = self.root / scope
            if base.is_dir():
                for d in sorted(base.iterdir()):
                    if (d / SEALED).exists() or (
                        scope == "conversation" and self.keys is not None
                        and (d / ".git").exists() and self.keys.is_locked(d.name)
                    ):  # fmt: skip
                        out.append((scope, d.name, LOCKED_FILES))
                    elif (d / ".git").exists():
                        out.append((scope, d.name, len(self.files(scope, d.name))))
        return out

    # ------------------------------------------------------------------ replication

    def head(self, owner: str) -> str | None:
        """The conversation repository's latest commit, if it has one and isn't sealed."""
        repo = self.repo("conversation", owner)
        if not (repo / ".git").exists():
            return None
        return (
            self._git(repo, "rev-parse", "--verify", "-q", "HEAD", check=False).decode().strip()
            or None
        )

    def bundle(self, owner: str) -> bytes:
        """The conversation repository's whole history, as one file."""
        repo = self.repo("conversation", owner)
        with tempfile.TemporaryDirectory(prefix="cw-art-", dir=_private_tmp()) as tmp:
            target = Path(tmp) / "history.bundle"
            self._git(repo, "bundle", "create", "--quiet", str(target), "main")
            return target.read_bytes()

    def merge_in(self, owner: str, source: Path) -> str:
        """Bring another device's history (a bundle or a repository) into this one; returns
        the new head. Two devices merging the same two histories make the same commit."""
        self._create_parents("conversation")
        with self._working("conversation", owner, write=True) as repo:
            self._ensure(repo)
            self._git(repo, "-c", "transfer.fsckObjects=true", "fetch", "--quiet", str(source),
                      "+main:refs/sync/incoming")  # fmt: skip
            theirs = self._git(repo, "rev-parse", "refs/sync/incoming").decode().strip()
            their_files = self._tree(repo, theirs)  # refuses what a write could be tricked by
            ours = self._git(repo, "rev-parse", "--verify", "-q", "HEAD",
                             check=False).decode().strip()  # fmt: skip
            base = ""
            if ours:
                base = self._git(repo, "merge-base", ours, theirs, check=False).decode().strip()
            if ours and base == theirs:
                return ours
            if not ours or base == ours:
                self._git(repo, "update-ref", "refs/heads/main", theirs)
                self._git(repo, "reset", "--quiet", "--hard", "main")
                return theirs
            merged = self._merge_commit(repo, ours, theirs, base, their_files)
            self._git(repo, "update-ref", "refs/heads/main", merged)
            self._git(repo, "reset", "--quiet", "--hard", "main")
            return merged

    def _tree(self, repo: Path, rev: str) -> dict[str, tuple[str, str]]:
        """{path: (mode, blob)}. Only ordinary files with ordinary paths: a symlink in history
        from another device could make a later write land outside the repository."""
        out = self._git(repo, "ls-tree", "-r", "-z", rev).decode()
        files = {}
        for line in filter(None, out.split("\0")):
            info, path = line.split("\t", 1)
            mode, _kind, blob = info.split(" ")
            if mode not in ("100644", "100755"):
                raise ArtifactError("history from another device holds something other than files")
            clean_path(path)
            files[path] = (mode, blob)
        return files

    def _merge_commit(self, repo: Path, ours: str, theirs: str, base: str,
                      their_files: dict[str, tuple[str, str]]) -> str:  # fmt: skip
        """Three-way, file by file. Both sides changed a file: one copy stays at its path (the
        side with the greater commit id, so every device picks the same), and the other is
        kept next to it. The commit's date and parents don't depend on who merges."""
        low, high = sorted((ours, theirs))
        a, b = self._tree(repo, low), their_files if high == theirs else self._tree(repo, high)
        if low == theirs:
            a = their_files
        old = self._tree(repo, base) if base else {}
        merged: dict[str, tuple[str, str]] = {}
        for path in sorted({*a, *b}):
            mine, other, was = a.get(path), b.get(path), old.get(path)
            if other == mine or mine == was:
                chosen = other
            elif other == was:
                chosen = mine
            else:
                chosen = other or mine
                if other is not None and mine is not None:
                    merged[f"{path}.from-{low[:10]}"] = mine
            if chosen is not None:
                merged[path] = chosen
        date = max(
            int(self._git(repo, "log", "-1", "--format=%ct", r).decode()) for r in (low, high)
        )
        stamp = f"{date} +0000"
        with tempfile.TemporaryDirectory(prefix="cw-art-", dir=_private_tmp()) as tmp:
            index = {"GIT_INDEX_FILE": str(Path(tmp) / "index")}
            lines = "".join(f"{m} {blob}\t{p}\n" for p, (m, blob) in sorted(merged.items()))
            self._git(repo, "update-index", "--add", "--index-info", input_bytes=lines.encode(),
                      extra_env=index)  # fmt: skip
            tree = self._git(repo, "write-tree", extra_env=index).decode().strip()
        return self._git(repo, "commit-tree", tree, "-p", low, "-p", high, "-m",
                         "merge from another device",
                         extra_env={"GIT_AUTHOR_DATE": stamp, "GIT_COMMITTER_DATE": stamp}
                         ).decode().strip()  # fmt: skip

    def purge(self, scope: Scope, owner: str) -> bool:
        repo = self.repo(scope, owner)
        if not repo.exists():
            return False
        remove_tree(repo)
        return True


def _item_id(scope: Scope, owner: str) -> str:
    return f"{scope}:{owner}"


def _folders(data_dir: Path, scope: Scope, owner: str) -> list[Path]:
    store = ArtifactStore(data_dir)
    return [store.repo(scope, owner), *([data_dir / "jobs" / owner] if scope == "job" else [])]


def _seal_folder(folder: Path, key: bytes) -> None:
    if folder.is_dir() and any(c.name != SEALED for c in folder.iterdir()):
        if not (folder / SEALED).exists():
            _write_atomic(folder / SEALED, crypto.seal(key, _pack(folder)))
        _remove_plaintext(folder)


def _unseal_folder(folder: Path, key: bytes) -> None:
    if (folder / SEALED).exists():
        data = crypto.open_sealed(key, (folder / SEALED).read_bytes())
        _remove_plaintext(folder)
        _unpack(data, folder)
        (folder / SEALED).unlink()


def _job_stub(job: dict[str, object]) -> str:
    """What's left of a locked job: not its question, library, notes or results."""
    keep = ("id", "kind", "status", "created", "not_before", "started", "finished", "actor")
    return json.dumps({**{k: job.get(k) for k in keep}, "params": {"locked": True}, "steps": [],
                       "error": None}, indent=1)  # fmt: skip


def lock_item(vault: Any, data_dir: Path, scope: Scope, owner: str, secret: str) -> None:
    """Lock a finished job's or a run's stored files with a passphrase (FR-ENC-1). The key is
    saved first: a crash midway can always be undone with `unlock_item`."""
    if scope == "conversation":
        raise ArtifactError("lock a conversation with `cairnward vault lock`")
    folders = _folders(data_dir, scope, owner)
    job_file = data_dir / "jobs" / owner / "job.json"
    job = None
    if scope == "job":
        if not job_file.exists():
            raise ArtifactError(f"no job {owner}")
        job = json.loads(job_file.read_text(encoding="utf-8"))
        if job.get("status") not in ("done", "failed", "cancelled"):
            raise ArtifactError("this job hasn't finished: lock it when it has")
    elif not (folders[0] / ".git").exists():
        raise ArtifactError(f"no artifacts for {scope} {owner}")
    key = vault.lock_item(_item_id(scope, owner), secret)
    with _seal_lock:
        for folder in folders:
            _seal_folder(folder, key)
        if job is not None:
            job_file.write_text(_job_stub(job), encoding="utf-8")


def unlock_item(vault: Any, data_dir: Path, scope: Scope, owner: str, secret: str) -> None:
    """Remove the lock and restore the plain files. The key goes last, once they're back."""
    item = _item_id(scope, owner)
    key = vault.open_item(item, secret)
    with _seal_lock:
        for folder in _folders(data_dir, scope, owner):
            _unseal_folder(folder, key)
    vault.remove_item(item)


def _pack(repo: Path) -> bytes:
    buf = io.BytesIO()
    with tarfile.open(fileobj=buf, mode="w") as tar:
        for child in sorted(repo.iterdir()):
            if child.name != SEALED:
                tar.add(child, arcname=child.name)
    return buf.getvalue()


def _unpack(data: bytes, dest: Path) -> None:
    with tarfile.open(fileobj=io.BytesIO(data), mode="r") as tar:
        tar.extractall(dest, filter="data")


def _write_atomic(path: Path, data: bytes) -> None:
    tmp = path.with_suffix(".tmp")
    with tmp.open("wb") as fh:
        fh.write(data)
        fh.flush()
        os.fsync(fh.fileno())  # needs a writable file on Windows
    if os.name != "nt":
        tmp.chmod(0o600)
    tmp.replace(path)


def _remove_plaintext(repo: Path) -> None:
    """Everything in a repository's folder except the sealed bundle."""
    if repo.is_dir():
        for child in repo.iterdir():
            if child.name != SEALED:
                remove_tree(child) if child.is_dir() else child.unlink()


def _private_tmp() -> str | None:
    """A memory-backed folder when there is one, so plaintext stays off the disk."""
    runtime = os.environ.get("XDG_RUNTIME_DIR")
    return runtime if runtime and Path(runtime).is_dir() else None


def remove_tree(path: Path) -> None:
    """rmtree that also removes read-only files (git objects on Windows)."""
    import stat

    def writable_then_retry(func: object, target: str, _exc: object) -> None:
        t = Path(target)
        t.chmod(stat.S_IWRITE)
        if t.is_file():
            t.unlink()
        else:
            t.rmdir()

    shutil.rmtree(path, onexc=writable_then_retry)
