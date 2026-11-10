"""The database key for SQLCipher mode (ADR-0019, option c).

One random 256-bit key encrypts vault.db, keys.db and audit.db. Its source of truth is
`<data_dir>/db-key.age`, the key wrapped with a passphrase (age scrypt, as for a locked
conversation). Optionally, `[storage] key = "env:NAME" | "keyring:NAME"` in cairnward.toml holds the
key itself (64 hex characters) so an unattended host can start without the passphrase; that's a
weakening, shown as a warning and recorded in settings snapshots.

`key_for(paths)` finds the key in this order: this process's memory; the `[storage] key`
reference; the passphrase, asked on a terminal. With no `db-key.age` the databases are plain and
the answer is None.

The sandboxed core in isolated mode can't be asked: `serve` writes the key to the core's standard
input, a pipe read once at start (`receive`). Not an environment variable: removing one from
`os.environ` leaves it readable in /proc/<pid>/environ for the life of the process (threat-model
review 2026-11-10, R1).
"""

from __future__ import annotations

import getpass
import os
import secrets
import sys
import tomllib
from pathlib import Path

from cairnward.config.paths import Paths
from cairnward.store import crypto
from cairnward.store.db import KEY_BYTES, DatabaseKeyError

KEY_FILE = "db-key.age"
ON_STDIN = "CAIRNWARD_DATABASE_KEY_ON_STDIN"  # set by serve: the key comes on stdin
DATABASES = ("vault.db", "keys.db", "audit.db")

_cache: dict[Path, bytes] = {}


def key_file(paths: Paths) -> Path:
    return paths.data_dir / KEY_FILE


def enabled(paths: Paths) -> bool:
    return key_file(paths).exists()


def configured_reference(paths: Paths) -> str | None:
    """`[storage] key` from cairnward.toml, read without loading the whole configuration (the
    stores open before it, and from commands that don't load it)."""
    path = paths.config_dir / "cairnward.toml"
    if not path.exists():
        return None
    try:
        data = tomllib.loads(path.read_text(encoding="utf-8"))
    except (OSError, tomllib.TOMLDecodeError):
        return None
    ref = (data.get("storage") or {}).get("key")
    return ref if isinstance(ref, str) and ref else None


def _parse(text: str, where: str) -> bytes:
    try:
        key = bytes.fromhex(text.strip())
    except ValueError:
        raise DatabaseKeyError(f"{where} isn't a database key (64 hex characters)") from None
    if len(key) != KEY_BYTES:
        raise DatabaseKeyError(f"{where} isn't a database key (64 hex characters)")
    return key


def _from_reference(ref: str) -> bytes:
    from cairnward.backends.base import BackendError
    from cairnward.backends.registry import resolve_secret

    try:
        value = resolve_secret(ref)
    except BackendError as exc:
        raise DatabaseKeyError(f"[storage] key = {ref!r}: {exc}") from None
    if ref.startswith("env:"):
        # read once: programs this process starts later (plugin commands, restic) don't
        # inherit it (threat-model review 2026-11-10, R2)
        os.environ.pop(ref[4:], None)
    return _parse(value or "", f"[storage] key = {ref!r}")


def unwrap(paths: Paths, passphrase: str) -> bytes:
    try:
        key = crypto.unwrap_key(key_file(paths).read_bytes(), passphrase)
    except crypto.DecryptionError:
        raise DatabaseKeyError("wrong passphrase for the database key") from None
    return _parse(key.decode(errors="replace"), KEY_FILE)


def key_for(paths: Paths, *, ask: bool = True) -> bytes | None:
    """The database key, or None when the databases aren't encrypted."""
    if not enabled(paths):
        return None
    where = paths.data_dir.resolve()
    if where in _cache:
        return _cache[where]
    if ref := configured_reference(paths):
        key = _from_reference(ref)
    elif ask and sys.stdin.isatty():
        key = unwrap(paths, getpass.getpass("Passphrase for Cairnward's databases: "))
    else:
        raise DatabaseKeyError(
            "the databases are encrypted: run this from a terminal to type the passphrase, or set "
            "[storage] key (ADR-0019)"
        )
    _cache[where] = key
    return key


def receive(paths: Paths) -> None:
    """In the sandboxed core: take the key `serve` wrote to stdin, when it says it did."""
    if os.environ.pop(ON_STDIN, None) == "1":
        remember(paths, _parse(sys.stdin.readline(), "the key from serve"))


def remember(paths: Paths, key: bytes) -> None:
    _cache[paths.data_dir.resolve()] = key


def forget(paths: Paths) -> None:
    _cache.pop(paths.data_dir.resolve(), None)


def pending_file(paths: Paths) -> Path:
    return paths.data_dir / (KEY_FILE + ".pending")


def _write(path: Path, key: bytes, passphrase: str) -> None:
    tmp = path.with_name(path.name + ".tmp")
    tmp.write_bytes(crypto.wrap_key(key.hex().encode(), passphrase))
    if os.name != "nt":
        tmp.chmod(0o600)
    with tmp.open("rb") as fh:
        os.fsync(fh.fileno())
    tmp.replace(path)


def begin(paths: Paths, passphrase: str) -> bytes:
    """The key for an encryption in progress: a new one, saved wrapped as `db-key.age.pending`
    before any database is converted, so a crash halfway can't lose it; or, when a pending
    file is already there, that one (resuming). `commit` makes it the key."""
    if enabled(paths):
        raise DatabaseKeyError(f"{key_file(paths)} already exists: the databases are encrypted")
    pending = pending_file(paths)
    if pending.exists():
        try:
            key = crypto.unwrap_key(pending.read_bytes(), passphrase)
        except crypto.DecryptionError:
            raise DatabaseKeyError("wrong passphrase for the unfinished encryption") from None
        return _parse(key.decode(errors="replace"), pending.name)
    key = secrets.token_bytes(KEY_BYTES)
    paths.data_dir.mkdir(parents=True, exist_ok=True)
    _write(pending, key, passphrase)
    return key


def commit(paths: Paths, key: bytes) -> None:
    pending_file(paths).replace(key_file(paths))
    remember(paths, key)


def rewrap(paths: Paths, old: str, new: str) -> None:
    """Change the passphrase: the key, and so the databases, stay the same."""
    _write(key_file(paths), unwrap(paths, old), new)


def weakenings(paths: Paths) -> list[str]:
    """Warnings for the privacy views: a key held by reference is weaker than the passphrase."""
    ref = configured_reference(paths)
    if ref and enabled(paths):
        return [f"[storage] key = {ref!r}: the database key is held outside its passphrase, so "
                "anyone who can read that reference can open the encrypted databases"]  # fmt: skip
    if ref:
        return [
            "[storage] key is set, but the databases aren't encrypted (cairnward store encrypt)"
        ]
    return []
