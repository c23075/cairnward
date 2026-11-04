"""Encrypted backups with restic (spec §4.8, FR-SYNC-1/3/6/8; ADR-0007 as amended by D50).

What a snapshot holds: not the live database files but a **per-conversation export** built
in a staging folder (`<state>/backup/staging`):

    conversations/<id>/conversation.json   the conversation row and its messages, exactly as
                                           stored (ciphertext when encrypted)
    conversations/<id>/key.json            its key (wrapped when the conversation is locked)
    vault/folders.json, research/research.db, audit/, artifacts/, jobs/, mcp_pins.json,
    config/, MANIFEST.json

so that purging one conversation can remove it from **every** old snapshot: `restic
rewrite --forget --exclude conversations/<id> --exclude artifacts/conversation/<id>`, then
`restic prune`. Until that has succeeded the purge is listed as pending (`cairnward backup
status`) and retried on each run. Purged conversations (tombstones) are never exported.

restic always encrypts. The repository is a local folder (a second disk, a NAS or
Syncthing folder, or a cloud drive mounted locally, e.g. with rclone mount); restic is
run with no network settings at all. The password is generated into the state folder
(owner-only) unless `password_command` is set; without it backups can't be restored.
"""

from __future__ import annotations

import base64
import contextlib
import contextvars
import json
import os
import re
import shutil
import sqlite3
import subprocess
import tempfile
import time
from collections.abc import Callable, Iterator
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from cairnward import __version__
from cairnward.config.loader import Config

Log = Callable[[str], None]
MIN_RESTIC = (0, 15)  # `rewrite`
HOST = "cairnward"  # recorded in snapshots instead of this machine's name
TAG = "cairnward"
CLOUD_HINTS = (
    "dropbox",
    "google drive",
    "googledrive",
    "my drive",
    "onedrive",
    "icloud",
    "mobile documents",
    "nextcloud",
    "owncloud",
    "pcloud",
    "mega",
    "box sync",
)


class BackupError(Exception):
    pass


# ------------------------------------------------------------------ paths and settings


def backup_dir(cfg: Config) -> Path:
    return cfg.paths.state_dir / "backup"


def password_file(cfg: Config) -> Path:
    return backup_dir(cfg) / "password"


def pending_file(cfg: Config) -> Path:
    # in the data folder: the vault records purges there (the isolated core can write it)
    return cfg.paths.data_dir / "backup-purges.json"


REMOTE_KINDS = ("s3", "rest", "b2", "azure", "gs", "swift", "rclone")  # HTTP: through egress


def remote_kind(cfg: Config) -> str | None:
    """The restic backend of a remote repository ("s3", "rest"...), or None for a folder."""
    repo = cfg.main.backup.repository or ""
    m = re.match(r"^([a-z0-9]+):", repo)
    if m is None or re.match(r"^[A-Za-z]:[\\/]", repo):
        return None
    return m.group(1)


def repository(cfg: Config) -> Path | str:
    repo = cfg.main.backup.repository
    if not repo:
        raise BackupError('no backup repository: set [backup] repository = "/path/to/folder" '
                          "(or a remote such as s3:... or rest:...) in cairnward.toml")  # fmt: skip
    kind = remote_kind(cfg)
    if kind is None:
        return Path(repo).expanduser()
    if kind not in REMOTE_KINDS:
        raise BackupError(f"'{kind}:' repositories aren't supported ({kind} wouldn't go through "
                          f"Cairnward's egress); use a folder or one of: "
                          f"{', '.join(k + ':' for k in REMOTE_KINDS)}")  # fmt: skip
    return repo


_PROXY: contextvars.ContextVar[str | None] = contextvars.ContextVar("restic_proxy", default=None)


@contextlib.contextmanager
def remote_access(cfg: Config) -> Iterator[None]:
    """For a remote repository: restic's traffic goes through an egress session in the
    backup environment (its network policy applies; the egress report shows it). The
    server must be running. A folder repository needs nothing."""
    if remote_kind(cfg) is None:
        yield
        return
    from cairnward.run.runner import RunError, egress_session

    env = cfg.main.backup.environment or cfg.main.defaults.environment
    try:
        with egress_session(cfg, env, "restic") as proxy:
            token = _PROXY.set(proxy)
            try:
                yield
            finally:
                _PROXY.reset(token)
    except RunError as exc:
        raise BackupError(str(exc)) from None


def cloud_warning(path: Path) -> str | None:
    lowered = str(path).lower()
    if any(h in lowered for h in CLOUD_HINTS):
        return (f"{path} looks like a cloud-synced folder: the contents are encrypted by "
                "restic, but the provider sees file sizes and times")  # fmt: skip
    return None


def restic() -> str | None:
    exe = shutil.which("restic")
    if not exe:
        return None
    try:
        out = subprocess.run([exe, "version"], capture_output=True, text=True, timeout=30,  # noqa: S603
                             check=True).stdout  # fmt: skip
    except (OSError, subprocess.SubprocessError):
        return None
    m = re.search(r"restic (\d+)\.(\d+)", out)
    return exe if m and (int(m.group(1)), int(m.group(2))) >= MIN_RESTIC else None


def _env(cfg: Config) -> dict[str, str]:
    """Only what restic needs: no proxy variables, no inherited secrets."""
    env = {"PATH": os.environ.get("PATH", ""), "HOME": os.environ.get("HOME", ""),
           "RESTIC_REPOSITORY": str(repository(cfg)),
           "RESTIC_CACHE_DIR": str(backup_dir(cfg) / "cache"),
           "TMPDIR": tempfile.gettempdir(), "RESTIC_PROGRESS_FPS": "0.2"}  # fmt: skip
    if os.name == "nt":
        env["SYSTEMROOT"] = os.environ.get("SYSTEMROOT", "")
    if proxy := _PROXY.get():
        env.update(HTTPS_PROXY=proxy, HTTP_PROXY=proxy)
    if cfg.main.backup.password_command:
        env["RESTIC_PASSWORD_COMMAND"] = cfg.main.backup.password_command
    else:
        env["RESTIC_PASSWORD_FILE"] = str(password_file(cfg))
    return env


def run_restic(cfg: Config, *args: str, cwd: Path | None = None, timeout: float = 6 * 3600,
               check: bool = True) -> subprocess.CompletedProcess[str]:  # fmt: skip
    exe = restic()
    if exe is None:
        raise BackupError("restic (0.15 or newer) isn't installed: e.g. sudo apt install restic")
    res = subprocess.run([exe, *args], env=_env(cfg), cwd=cwd, capture_output=True, text=True,  # noqa: S603
                         timeout=timeout, check=False)  # fmt: skip
    if check and res.returncode != 0:
        raise BackupError(f"restic {args[0]} failed: {(res.stderr or res.stdout).strip()[-500:]}")
    return res


@contextlib.contextmanager
def _locked(cfg: Config) -> Iterator[None]:
    """One backup operation at a time (CLI and serve may both try)."""
    d = backup_dir(cfg)
    d.mkdir(parents=True, exist_ok=True)
    fh = (d / "lock").open("a+")
    try:
        try:
            import fcntl

            fcntl.flock(fh, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except ImportError:  # Windows: no lock (backups are Linux-first)
            pass
        except BlockingIOError:
            raise BackupError("another backup is running") from None
        yield
    finally:
        fh.close()


def _audit(cfg: Config, action: str, outcome: str = "ok", **details: Any) -> None:
    from cairnward.audit import AuditLog, enabled_for

    with contextlib.suppress(Exception), AuditLog.open(cfg.paths, enabled_for(cfg)) as audit:
        audit.append(action, actor="cairnward", outcome=outcome, details=details)


# ------------------------------------------------------------------ init


def initialised(cfg: Config) -> bool:
    repo = repository(cfg)
    if isinstance(repo, Path):
        return (repo / "config").is_file()
    return run_restic(cfg, "cat", "config", check=False, timeout=120).returncode == 0


def init(cfg: Config, log: Log = print) -> str | None:
    """Create the repository (and the password, unless password_command is set). Returns
    the new password so it can be shown once, or None if it already existed."""
    repo = repository(cfg)
    if initialised(cfg):
        log(f"backup repository already set up at {repo}")
        return None
    new_password = None
    if not cfg.main.backup.password_command:
        pw = password_file(cfg)
        if not pw.exists():
            pw.parent.mkdir(parents=True, exist_ok=True)
            if os.name != "nt":
                pw.parent.chmod(0o700)
            import secrets

            new_password = secrets.token_urlsafe(32)
            pw.write_text(new_password + "\n", encoding="utf-8")
            if os.name != "nt":
                pw.chmod(0o600)
    if isinstance(repo, Path):
        repo.parent.mkdir(parents=True, exist_ok=True)
    run_restic(cfg, "init")
    _audit(cfg, "backup.init", repository=str(repo))
    if warning := (cloud_warning(repo) if isinstance(repo, Path) else
                   f"{repo} is a remote repository: its contents are encrypted by restic, "
                   "but the provider sees sizes and times"):  # fmt: skip
        log(f"note: {warning}")
    return new_password


# ------------------------------------------------------------------ staging


def _jsonable(value: Any) -> Any:
    if isinstance(value, bytes | memoryview):
        return {"$b64": base64.b64encode(bytes(value)).decode()}
    return value


def _unjson(value: Any) -> Any:
    if isinstance(value, dict) and set(value) == {"$b64"}:
        return base64.b64decode(value["$b64"])
    return value


def _rows(conn: sqlite3.Connection, sql: str, *params: Any) -> list[dict[str, Any]]:
    cur = conn.execute(sql, params)
    cols = [c[0] for c in cur.description]
    return [{c: _jsonable(v) for c, v in zip(cols, row, strict=True)} for row in cur]


def _ro(path: Path) -> sqlite3.Connection:
    return sqlite3.connect(f"file:{path.as_posix()}?mode=ro", uri=True)


def _copy_db(src: Path, dst: Path) -> None:
    dst.parent.mkdir(parents=True, exist_ok=True)
    with contextlib.closing(_ro(src)) as s, contextlib.closing(sqlite3.connect(dst)) as d:
        s.backup(d)  # a consistent copy, even while Cairnward writes


def stage(cfg: Config, dest: Path) -> dict[str, Any]:
    """Build the per-conversation export in `dest` (emptied first)."""
    from cairnward.artifacts import remove_tree

    data, b = cfg.paths.data_dir, cfg.main.backup
    if dest.exists():
        remove_tree(dest)
    dest.mkdir(parents=True)
    if os.name != "nt":
        dest.chmod(0o700)
    counts: dict[str, int] = {"conversations": 0, "messages": 0}
    vault_db, keys_db = data / "vault.db", data / "keys.db"
    if vault_db.exists():
        with (
            contextlib.closing(_ro(vault_db)) as v,
            contextlib.closing(
                _ro(keys_db) if keys_db.exists() else sqlite3.connect(":memory:")
            ) as k,
        ):
            v.execute("BEGIN")  # one consistent read
            (dest / "vault").mkdir()
            folders = _rows(v, "SELECT * FROM folders")
            (dest / "vault" / "folders.json").write_text(json.dumps(folders), encoding="utf-8")
            has_keys = bool(k.execute("SELECT name FROM sqlite_master WHERE type='table' AND "
                                      "name='conversation_keys'").fetchone())  # fmt: skip
            for conv in _rows(v, "SELECT * FROM conversations WHERE purged_at IS NULL"):
                folder = dest / "conversations" / conv["id"]
                folder.mkdir(parents=True)
                msgs = _rows(v, "SELECT * FROM messages WHERE conversation_id = ? ORDER BY seq",
                             conv["id"])  # fmt: skip
                (folder / "conversation.json").write_text(
                    json.dumps({"conversation": conv, "messages": msgs}), encoding="utf-8"
                )
                if has_keys:
                    keys = _rows(k, "SELECT * FROM conversation_keys WHERE conversation_id = ?",
                                 conv["id"])  # fmt: skip
                    if keys:
                        (folder / "key.json").write_text(json.dumps(keys[0]), encoding="utf-8")
                counts["conversations"] += 1
                counts["messages"] += len(msgs)
            if has_keys:  # locked jobs, runs, libraries: useless without their wrapped keys
                items = _rows(k, "SELECT * FROM conversation_keys WHERE conversation_id LIKE '%:%'")
                if items:
                    (dest / "locked-items").mkdir()
                    (dest / "locked-items" / "keys.json").write_text(json.dumps(items),
                                                                     encoding="utf-8")  # fmt: skip
    if b.include_research and (data / "research.db").exists():
        _copy_db(data / "research.db", dest / "research" / "research.db")
        if (data / "library-locks").is_dir():
            shutil.copytree(data / "library-locks", dest / "research" / "library-locks")
    if (data / "audit.db").exists():
        _copy_db(data / "audit.db", dest / "audit" / "audit.db")
    if (data / "audit.jsonl").exists():
        shutil.copy2(data / "audit.jsonl", dest / "audit" / "audit.jsonl")
    if b.include_artifacts and (data / "artifacts").is_dir():
        shutil.copytree(data / "artifacts", dest / "artifacts")
    if (data / "jobs").is_dir():
        shutil.copytree(data / "jobs", dest / "jobs")
    if (data / "mcp_pins.json").exists():
        shutil.copy2(data / "mcp_pins.json", dest / "mcp_pins.json")
    if b.include_config and cfg.paths.config_dir.is_dir():
        shutil.copytree(cfg.paths.config_dir, dest / "config")
    manifest = {"format": 1, "cairnward": __version__,
                "created": datetime.now(UTC).isoformat(timespec="seconds"), **counts}  # fmt: skip
    (dest / "MANIFEST.json").write_text(json.dumps(manifest, indent=1), encoding="utf-8")
    return manifest


# ------------------------------------------------------------------ backup


def snapshots(cfg: Config) -> list[dict[str, Any]]:
    out = run_restic(cfg, "snapshots", "--json", "--tag", TAG).stdout
    return sorted(json.loads(out or "[]"), key=lambda s: s["time"])


def last_run(cfg: Config) -> dict[str, Any] | None:
    f = backup_dir(cfg) / "last.json"
    with contextlib.suppress(OSError, ValueError):
        return dict(json.loads(f.read_text(encoding="utf-8")))
    return None


def run(cfg: Config, log: Log = print) -> dict[str, Any]:
    """Purge what's pending from old snapshots, then take a new one and apply retention."""
    if not initialised(cfg):
        raise BackupError("the backup repository isn't set up yet: cairnward backup init")
    with _locked(cfg):
        started = time.monotonic()
        _process_purges(cfg, log)
        staging = backup_dir(cfg) / "staging"
        manifest = stage(cfg, staging)
        log(f"backing up {manifest['conversations']} conversations ...")
        try:
            res = run_restic(cfg, "backup", "--json", "--host", HOST, "--tag", TAG, ".",
                             cwd=staging)  # fmt: skip
        except BackupError as exc:
            _audit(cfg, "backup.run", "error", error=str(exc)[:300])
            raise
        summary: dict[str, Any] = {}
        for line in res.stdout.splitlines():
            with contextlib.suppress(ValueError):
                msg = json.loads(line)
                if msg.get("message_type") == "summary":
                    summary = msg
        b = cfg.main.backup
        run_restic(cfg, "forget", "--prune", "--host", HOST, "--tag", TAG,
                   "--keep-daily", str(b.keep_daily), "--keep-weekly", str(b.keep_weekly),
                   "--keep-monthly", str(b.keep_monthly), "--keep-last", "1")  # fmt: skip
        from cairnward.artifacts import remove_tree

        remove_tree(staging)  # the export is plain files; don't leave it lying around
        result = {"snapshot": summary.get("snapshot_id", "")[:12],
                  "conversations": manifest["conversations"],
                  "files": summary.get("total_files_processed"),
                  "bytes_added": summary.get("data_added"),
                  "seconds": round(time.monotonic() - started, 1),
                  "time": datetime.now(UTC).isoformat(timespec="seconds")}  # fmt: skip
        (backup_dir(cfg) / "last.json").write_text(json.dumps(result), encoding="utf-8")
        _audit(cfg, "backup.run", **result)
        return result


def run_now(cfg: Config, log: Log = lambda _m: None) -> dict[str, Any]:
    """A backup asked for by hand (the dashboard): sets up the repository the first time."""
    b = cfg.main.backup
    if b.mode == "none" or not b.repository:
        raise BackupError("backups are off: set a mode and a repository first")
    with remote_access(cfg):
        if not initialised(cfg):
            init(cfg, log)
        return run(cfg, log)


def due(cfg: Config, now: float | None = None) -> bool:
    last = last_run(cfg)
    if last is None:
        return True
    when = datetime.fromisoformat(last["time"]).timestamp()
    return (now or time.time()) - when >= cfg.main.backup.interval_hours * 3600


# ------------------------------------------------------------------ purging from backups


def request_purge(data_dir: Path, conversation_id: str) -> None:
    """Called by the vault when a conversation is purged: remove it from old backups too."""
    f = data_dir / "backup-purges.json"
    items = _read_pending(f)
    if not any(i["id"] == conversation_id for i in items):
        items.append({"id": conversation_id,
                      "requested": datetime.now(UTC).isoformat(timespec="seconds"),
                      "attempts": 0})  # fmt: skip
        _write_pending(f, items)


def _read_pending(f: Path) -> list[dict[str, Any]]:
    with contextlib.suppress(OSError, ValueError):
        data = json.loads(f.read_text(encoding="utf-8"))
        if isinstance(data, list):
            return [i for i in data if isinstance(i, dict) and "id" in i]
    return []


def _write_pending(f: Path, items: list[dict[str, Any]]) -> None:
    f.parent.mkdir(parents=True, exist_ok=True)
    tmp = f.with_suffix(".tmp")
    tmp.write_text(json.dumps(items, indent=1), encoding="utf-8")
    tmp.replace(f)


def pending(cfg: Config) -> list[dict[str, Any]]:
    return _read_pending(pending_file(cfg))


def process_purges(cfg: Config, log: Log = print) -> int:
    """Remove pending purged conversations from every snapshot; returns how many."""
    with _locked(cfg):
        return _process_purges(cfg, log)


def _process_purges(cfg: Config, log: Log) -> int:
    items = pending(cfg)
    if not items:
        return 0
    if not cfg.main.backup.repository or not initialised(cfg):
        if cfg.main.backup.mode == "none" and not cfg.main.backup.repository:
            _write_pending(pending_file(cfg), [])  # no backups, so nothing to remove
        return 0
    ids = [i["id"] for i in items]
    patterns = [p for i in ids for p in (f"conversations/{i}", f"artifacts/conversation/{i}")]
    excludes = backup_dir(cfg) / "purge-excludes.txt"
    excludes.write_text("\n".join(patterns) + "\n", encoding="utf-8")
    try:
        run_restic(cfg, "rewrite", "--forget", "--tag", TAG, "--exclude-file", str(excludes))
        run_restic(cfg, "prune")
        left = remaining(cfg, ids)
        if left:
            raise BackupError(f"still in backups after rewrite: {', '.join(left)}")
    except BackupError as exc:
        for i in items:
            i["attempts"] = int(i.get("attempts", 0)) + 1
            i["last_error"] = str(exc)[:300]
        _write_pending(pending_file(cfg), items)
        _audit(cfg, "backup.purge", "error", conversations=len(ids), error=str(exc)[:300])
        log(f"couldn't remove {len(ids)} purged conversation(s) from backups yet: {exc}")
        return 0
    finally:
        excludes.unlink(missing_ok=True)
    _write_pending(pending_file(cfg), [])
    for i in ids:
        from cairnward.audit import AuditLog, enabled_for

        with contextlib.suppress(Exception), AuditLog.open(cfg.paths, enabled_for(cfg)) as audit:
            audit.append("backup.purge", actor="cairnward", outcome="ok", conversation_id=i,
                         details={"snapshots_rewritten": True})  # fmt: skip
    log(f"removed {len(ids)} purged conversation(s) from every backup snapshot")
    return len(ids)


def remaining(cfg: Config, ids: list[str]) -> list[str]:
    """Which of these conversations any snapshot still contains."""
    left = []
    for i in ids:
        out = run_restic(cfg, "find", "--json", "--tag", TAG, i).stdout
        found = json.loads(out or "[]")
        if any(m.get("matches") for m in found):
            left.append(i)
    return left


# ------------------------------------------------------------------ restore


def pick(cfg: Config, *, snapshot: str | None = None, at: str | None = None) -> dict[str, Any]:
    snaps = snapshots(cfg)
    if not snaps:
        raise BackupError("there are no backups yet")
    if snapshot:
        for s in snaps:
            if s["id"].startswith(snapshot) or s.get("short_id") == snapshot:
                return s
        raise BackupError(f"no snapshot {snapshot}")
    if at:
        limit = at if "T" in at else f"{at}T23:59:59"
        older = [s for s in snaps if s["time"][:19] <= limit[:19]]
        if not older:
            raise BackupError(f"no backup from {at} or earlier")
        return older[-1]
    return snaps[-1]


def restore(cfg: Config, target: Path, *, snapshot: str | None = None,
            at: str | None = None) -> tuple[dict[str, Any], Path]:  # fmt: skip
    """Extract a snapshot's export into `target`; returns (snapshot, folder with it)."""
    snap = pick(cfg, snapshot=snapshot, at=at)
    target.mkdir(parents=True, exist_ok=True)
    run_restic(cfg, "restore", snap["id"], "--target", str(target))  # backed up as "."
    if not (target / "MANIFEST.json").exists():
        found = next(iter(sorted(target.rglob("MANIFEST.json"), key=lambda p: len(p.parts))), None)
        if found is None:
            raise BackupError(
                "the restored snapshot has no MANIFEST.json (not a Cairnward backup?)"
            )
        target = found.parent
    _audit(cfg, "backup.restore", snapshot=snap["short_id"], time=snap["time"])
    return snap, target


def import_conversations(cfg: Config, export: Path) -> dict[str, int]:
    """Add the conversations in a restored export that the vault doesn't have (nothing
    existing is overwritten; purged ones stay purged)."""
    from cairnward.store import Vault

    Vault.open(cfg.paths).close()  # make sure the databases exist and are migrated
    data = cfg.paths.data_dir
    added = skipped = 0
    with contextlib.closing(sqlite3.connect(data / "vault.db")) as v, contextlib.closing(
            sqlite3.connect(data / "keys.db")) as k:  # fmt: skip
        folders_file = export / "vault" / "folders.json"
        if folders_file.exists():
            for row in json.loads(folders_file.read_text(encoding="utf-8")):
                _insert(v, "folders", row)
        for folder in sorted((export / "conversations").glob("*")):
            doc = json.loads((folder / "conversation.json").read_text(encoding="utf-8"))
            conv = doc["conversation"]
            if v.execute("SELECT 1 FROM conversations WHERE id = ?", (conv["id"],)).fetchone():
                skipped += 1  # present, or purged (the tombstone stays)
                continue
            _insert(v, "conversations", conv)
            for m in doc["messages"]:
                _insert(v, "messages", m)
            key = folder / "key.json"
            if key.exists():
                _insert(k, "conversation_keys", json.loads(key.read_text(encoding="utf-8")))
            added += 1
        items = export / "locked-items" / "keys.json"  # locked jobs, runs, libraries
        if items.exists():
            for row in json.loads(items.read_text(encoding="utf-8")):
                if not k.execute("SELECT 1 FROM conversation_keys WHERE conversation_id = ?",
                                 (row["conversation_id"],)).fetchone():  # fmt: skip
                    _insert(k, "conversation_keys", row)
        k.commit()
        v.commit()
    _audit(cfg, "backup.import", added=added, skipped=skipped)
    return {"added": added, "skipped": skipped}


def _insert(conn: sqlite3.Connection, table: str, row: dict[str, Any]) -> None:
    cols = list(row)
    conn.execute(f"INSERT OR IGNORE INTO {table} ({', '.join(cols)}) "  # noqa: S608 - own schema
                 f"VALUES ({', '.join('?' for _ in cols)})",
                 [_unjson(row[c]) for c in cols])  # fmt: skip


# ------------------------------------------------------------------ status


def status(cfg: Config) -> dict[str, Any]:
    b = cfg.main.backup
    out: dict[str, Any] = {"mode": b.mode, "repository": b.repository, "restic": restic(),
                           "pending_purges": len(pending(cfg)), "last": last_run(cfg)}  # fmt: skip
    try:
        out["initialised"] = initialised(cfg)
        if out["initialised"] and out["restic"]:
            out["snapshots"] = len(snapshots(cfg))
    except BackupError as exc:
        out["problem"] = str(exc)
    if b.repository:
        out["cloud_warning"] = cloud_warning(Path(b.repository).expanduser())
    return out
