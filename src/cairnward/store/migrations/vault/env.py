"""Alembic environment: runs on the connection Cairnward passes in (no alembic.ini)."""

from typing import Any

from alembic import context

from cairnward.store.db import METADATA, unmanaged_table

config = context.config
connection = config.attributes["connection"]


def include_object(
    obj: Any, name: str | None, type_: str, reflected: bool, compare_to: Any
) -> bool:
    # FTS5 and sqlite-vec tables are created by hand, not from the metadata.
    return not (type_ == "table" and name is not None and unmanaged_table(name))


context.configure(
    connection=connection,
    target_metadata=METADATA[config.get_main_option("cairnward_db")],  # type: ignore[index]
    render_as_batch=True,  # SQLite needs batch mode for ALTER TABLE
    include_object=include_object,
)
with context.begin_transaction():
    context.run_migrations()
