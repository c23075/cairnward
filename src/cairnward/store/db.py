"""Engines, SQLite settings and migrations for vault.db and keys.db."""

from __future__ import annotations

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
    "PRAGMA busy_timeout=5000",
)


def make_engine(path: Path) -> Engine:
    path.parent.mkdir(parents=True, exist_ok=True)
    engine = create_engine(f"sqlite:///{path}")

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
