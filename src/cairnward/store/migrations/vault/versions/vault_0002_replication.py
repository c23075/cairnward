"""replication

Revision ID: vault_0002
Revises: vault_0001
Create Date: 2026-11-04 12:00:00.000000
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "vault_0002"
down_revision: str | None = "vault_0001"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    with op.batch_alter_table("messages") as batch:
        batch.add_column(sa.Column("lamport", sa.Integer(), nullable=True))
        batch.add_column(sa.Column("origin", sa.String(length=32), nullable=True))
        batch.create_index("ix_messages_lamport", ["lamport"], unique=False)
    op.create_table(
        "sync_state",
        sa.Column("name", sa.String(length=32), nullable=False),
        sa.Column("value", sa.Text(), nullable=False),
        sa.PrimaryKeyConstraint("name"),
    )
    op.create_table(
        "sync_items",
        sa.Column("kind", sa.String(length=8), nullable=False),
        sa.Column("id", sa.String(length=36), nullable=False),
        sa.Column("state", sa.Text(), nullable=False),
        sa.PrimaryKeyConstraint("kind", "id"),
    )
    op.create_table(
        "sync_files",
        sa.Column("device", sa.String(length=32), nullable=False),
        sa.Column("path", sa.String(length=64), nullable=False),
        sa.Column("offset", sa.Integer(), nullable=False),
        sa.Column("records", sa.Integer(), nullable=False),
        sa.PrimaryKeyConstraint("device", "path"),
    )


def downgrade() -> None:
    op.drop_table("sync_files")
    op.drop_table("sync_items")
    op.drop_table("sync_state")
    with op.batch_alter_table("messages") as batch:
        batch.drop_index("ix_messages_lamport")
        batch.drop_column("origin")
        batch.drop_column("lamport")
