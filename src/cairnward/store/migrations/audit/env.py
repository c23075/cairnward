"""Alembic environment: runs on the connection Cairnward passes in (no alembic.ini)."""

from alembic import context

from cairnward.store.db import METADATA

config = context.config
connection = config.attributes["connection"]
context.configure(
    connection=connection,
    target_metadata=METADATA[config.get_main_option("cairnward_db")],  # type: ignore[index]
    render_as_batch=True,  # SQLite needs batch mode for ALTER TABLE
)
with context.begin_transaction():
    context.run_migrations()
