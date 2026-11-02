"""recovery key (FR-ENC-1, optional)

Revision ID: keys_0002
Revises: keys_0001
Create Date: 2026-11-02 09:30:00
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "keys_0002"
down_revision: str | None = "keys_0001"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    with op.batch_alter_table("conversation_keys") as batch:
        batch.add_column(sa.Column("recovery", sa.LargeBinary(), nullable=True))
    op.create_table(
        "recovery_key",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("recipient", sa.Text(), nullable=False),
        sa.Column("created_at", sa.String(length=32), nullable=False),
        sa.PrimaryKeyConstraint("id"),
    )


def downgrade() -> None:
    op.drop_table("recovery_key")
    with op.batch_alter_table("conversation_keys") as batch:
        batch.drop_column("recovery")
