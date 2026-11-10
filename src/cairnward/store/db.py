"""Engines, SQLite settings and migrations for vault.db and keys.db.

Optionally (ADR-0019, "encrypt everything") a database is a SQLCipher file opened with a raw
256-bit key: `make_engine(path, key)` and `connect(path, key)`. SQLCipher encrypts every page,
so table and column names, sizes of rows, ids and timestamps are hidden too, not just the
message bodies the vault already encrypts. The driver (`sqlcipher3`, extra `sqlcipher`) exists
only for Linux x86-64; without a key, plain `sqlite3` is used as before.
"""

from __future__ import annotations

import os
import sqlite3
from pathlib import Path
from typing import TYPE_CHECKING, Any, Literal

from sqlalchemy import Engine, MetaData, create_engine, event, text
from sqlalchemy.exc import OperationalError

if TYPE_CHECKING:
    from alembic.config import Config as AlembicConfig

from cairnward.store.schema import AUDIT_METADATA, KEYS_METADATA, VAULT_METADATA

DbName = Literal["vault", "keys", "audit"]
MIGRATIONS = Path(__file__).parent / "migrations"
# The newest migration of each database. Alembic is imported only when a database isn't at
# this revision: importing it and loading the scripts on every start cost both processes about
# 0.1 s each (I15). tests/test_store.py checks these against alembic's own view of the scripts,
# so adding a migration without updating this fails the tests.
HEADS: dict[DbName, str] = {"vault": "vault_0002", "keys": "keys_0002", "audit": "audit_0001"}
METADATA: dict[DbName, MetaData] = {
    "vault": VAULT_METADATA,
    "keys": KEYS_METADATA,
    "audit": AUDIT_METADATA,
}


# WAL for concurrency; FULL sync so a power cut can't lose committed data (NFR-R1);
# secure_delete overwrites deleted rows in the file (crypto-shredding, best effort on SSDs).
PRAGMAS = (
    "PRAGMA journal_mode=WAL",
    "PRAGMA synchronous=FULL",
    "PRAGMA foreign_keys=ON",
    "PRAGMA secure_delete=ON",
    "PRAGMA busy_timeout=30000",  # another process's write, behind a slow fsync, may take seconds
)


KEY_BYTES = 32
_PLAIN_HEADER = b"SQLite format 3\x00"


class DatabaseKeyError(Exception):
    """SQLCipher is unavailable, or the key doesn't open the database."""


def _cipher() -> Any:
    try:
        from sqlcipher3 import dbapi2  # type: ignore[import-untyped, unused-ignore]
    except ImportError:
        raise DatabaseKeyError(
            "encrypted databases need SQLCipher: `uv sync --extra sqlcipher` (Linux x86-64 only)"
        ) from None
    return dbapi2


def _key_pragma(key: bytes) -> str:
    if len(key) != KEY_BYTES:
        raise DatabaseKeyError(f"a database key must be {KEY_BYTES} bytes")
    return f"PRAGMA key = \"x'{key.hex()}'\""  # a raw key: no passphrase derivation per connect


def is_encrypted(path: Path) -> bool:
    """True for an existing file that isn't a plain SQLite database (SQLCipher has no header)."""
    if not path.exists() or path.stat().st_size == 0:
        return False
    with path.open("rb") as fh:
        return fh.read(16) != _PLAIN_HEADER


def connect(path: Path, key: bytes | None = None, *, readonly: bool = False) -> Any:
    """A DB-API connection: plain sqlite3 without a key, SQLCipher with one. A wrong key, or a key
    for a plain file, raises DatabaseKeyError rather than failing later on the first query."""
    target = f"file:{path.as_posix()}?mode=ro" if readonly else str(path)
    if key is None:
        if is_encrypted(path):
            raise DatabaseKeyError(f"{path.name} is encrypted: its key is needed")
        return sqlite3.connect(target, uri=readonly, check_same_thread=False)
    mod = _cipher()
    conn = mod.connect(target, uri=readonly, check_same_thread=False)
    try:
        conn.execute(_key_pragma(key))
        conn.execute("SELECT count(*) FROM sqlite_master").fetchone()
    except mod.DatabaseError:
        conn.close()
        raise DatabaseKeyError(f"the key doesn't open {path.name} (wrong key, or a plain "
                               "database)") from None  # fmt: skip
    return conn


def make_engine(path: Path, key: bytes | None = None) -> Engine:
    path.parent.mkdir(parents=True, exist_ok=True)
    if key is None:
        engine = create_engine(f"sqlite:///{path}")
    else:
        # the URL picks SQLAlchemy's file-database pool; `creator` makes keyed connections
        # and `module` lets SQLAlchemy recognise (and wrap) the driver's own exceptions
        engine = create_engine(f"sqlite:///{path}", module=_cipher(),
                               creator=lambda: connect(path, key))  # fmt: skip

    @event.listens_for(engine, "connect")
    def _pragmas(dbapi_conn: Any, _record: Any) -> None:
        cur = dbapi_conn.cursor()
        for pragma in PRAGMAS:
            cur.execute(pragma)
        cur.close()

    return engine


def alembic_config(db: DbName) -> AlembicConfig:
    from alembic.config import Config as AlembicConfig

    cfg = AlembicConfig()
    cfg.set_main_option("script_location", str(MIGRATIONS / db))
    cfg.set_main_option("cairnward_db", db)
    return cfg


def stored_revisions(engine: Engine) -> list[str]:
    """The revisions recorded in the database, without alembic ([] for a new database)."""
    with engine.connect() as conn:
        try:
            rows = conn.execute(text("SELECT version_num FROM alembic_version")).fetchall()
        except OperationalError:  # no such table: a new database
            return []
    return sorted(str(r[0]) for r in rows)


def upgrade(engine: Engine, db: DbName) -> None:
    """Bring the database to the latest schema (creates it if new). A database already at the
    newest migration is left alone without loading alembic."""
    if stored_revisions(engine) == [HEADS[db]]:
        return
    from alembic import command

    cfg = alembic_config(db)
    with engine.begin() as conn:
        cfg.attributes["connection"] = conn
        command.upgrade(cfg, "head")


def current_revision(engine: Engine) -> str | None:
    from alembic.runtime.migration import MigrationContext

    with engine.connect() as conn:
        return MigrationContext.configure(conn).get_current_revision()


def head_revision(db: DbName) -> str | None:
    from alembic.script import ScriptDirectory

    return ScriptDirectory.from_config(alembic_config(db)).get_current_head()


def checkpoint(engine: Engine) -> None:
    """Fold the WAL into the main file and truncate it (after purges)."""
    with engine.connect() as conn:
        conn.execute(text("PRAGMA wal_checkpoint(TRUNCATE)"))


def _sql_text(value: str) -> str:
    return "'" + value.replace("'", "''") + "'"


def convert(path: Path, key: bytes, *, encrypt: bool) -> None:
    """Rewrite a database encrypted (or back to plain) with sqlcipher_export, then swap it in
    atomically. The caller must have stopped every other user of the file. The old file's pages
    are overwritten only as far as the filesystem allows (see limitations)."""
    if encrypt == is_encrypted(path):
        raise DatabaseKeyError(f"{path.name} is already {'encrypted' if encrypt else 'plain'}")
    mod = _cipher()
    tmp = path.with_name(path.name + ".converting")
    tmp.unlink(missing_ok=True)
    conn = mod.connect(str(path))
    try:
        if not encrypt:
            conn.execute(_key_pragma(key))
        conn.execute("PRAGMA wal_checkpoint(TRUNCATE)")
        version = conn.execute("PRAGMA user_version").fetchone()[0]
        target = _key_pragma(key).split("=", 1)[1].strip() if encrypt else "''"
        conn.execute(f"ATTACH DATABASE {_sql_text(str(tmp))} AS converted KEY {target}")
        conn.execute("SELECT sqlcipher_export('converted')")
        conn.execute(f"PRAGMA converted.user_version = {int(version)}")
        conn.execute("DETACH DATABASE converted")
    except mod.DatabaseError as exc:
        tmp.unlink(missing_ok=True)
        raise DatabaseKeyError(f"couldn't convert {path.name}: {exc}") from None
    finally:
        conn.close()
    with tmp.open("rb") as fh:
        os.fsync(fh.fileno())
    if os.name != "nt":
        tmp.chmod(0o600)
    for suffix in ("-wal", "-shm"):
        path.with_name(path.name + suffix).unlink(missing_ok=True)
    tmp.replace(path)
