"""Engines, SQLite settings and migrations for vault.db and keys.db."""

from __future__ import annotations

from pathlib import Path
from typing import Any, Literal

from alembic import command
from alembic.config import Config as AlembicConfig
from alembic.runtime.migration import MigrationContext
from alembic.script import ScriptDirectory
from sqlalchemy import Engine, MetaData, create_engine, event, text

from cairnward.store.schema import AUDIT_METADATA, KEYS_METADATA, VAULT_METADATA

DbName = Literal["vault", "keys", "audit"]
MIGRATIONS = Path(__file__).parent / "migrations"
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
    cfg = AlembicConfig()
    cfg.set_main_option("script_location", str(MIGRATIONS / db))
    cfg.set_main_option("cairnward_db", db)
    return cfg


def upgrade(engine: Engine, db: DbName) -> None:
    """Bring the database to the latest schema (creates it if new)."""
    cfg = alembic_config(db)
    with engine.begin() as conn:
        cfg.attributes["connection"] = conn
        command.upgrade(cfg, "head")


def current_revision(engine: Engine) -> str | None:
    with engine.connect() as conn:
        return MigrationContext.configure(conn).get_current_revision()


def head_revision(db: DbName) -> str | None:
    return ScriptDirectory.from_config(alembic_config(db)).get_current_head()


def checkpoint(engine: Engine) -> None:
    """Fold the WAL into the main file and truncate it (after purges)."""
    with engine.connect() as conn:
        conn.execute(text("PRAGMA wal_checkpoint(TRUNCATE)"))
