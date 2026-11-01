"""Table definitions for the two databases (SQLAlchemy Core).

vault.db  conversations, messages, folders. Content and titles are encrypted per
          conversation (age) unless crypto-shredding is switched off.
keys.db   one key per conversation. Kept separate and small so a purged key can be
          deleted and the file compacted quickly (crypto-shredding, ADR-0006).

Schema changes: edit these tables, then add an Alembic revision under
migrations/<db>/versions (tests/test_store.py fails if the two disagree).
"""

from __future__ import annotations

from sqlalchemy import (
    Boolean,
    Column,
    ForeignKey,
    Index,
    Integer,
    LargeBinary,
    MetaData,
    String,
    Table,
    Text,
)

VAULT_METADATA = MetaData()
KEYS_METADATA = MetaData()

folders = Table(
    "folders",
    VAULT_METADATA,
    Column("id", String(36), primary_key=True),
    Column("name", Text, nullable=False),
    Column("parent_id", String(36), ForeignKey("folders.id", ondelete="CASCADE")),
    Column("created_at", String(32), nullable=False),
)

conversations = Table(
    "conversations",
    VAULT_METADATA,
    Column("id", String(36), primary_key=True),
    Column("created_at", String(32), nullable=False),
    Column("updated_at", String(32), nullable=False),
    Column("folder_id", String(36), ForeignKey("folders.id", ondelete="SET NULL")),
    Column("environment", Text),
    Column("client", Text),
    Column("enc", String(8), nullable=False),  # "age" | "none"
    Column("title", LargeBinary),  # encrypted when enc = "age"
    Column("pinned", Boolean, nullable=False, default=False),
    Column("archived", Boolean, nullable=False, default=False),
    Column("deleted_at", String(32)),  # soft delete (trash)
    Column("purged_at", String(32)),  # tombstone: content and key destroyed
)

messages = Table(
    "messages",
    VAULT_METADATA,
    Column("id", String(36), primary_key=True),
    Column(
        "conversation_id",
        String(36),
        ForeignKey("conversations.id", ondelete="CASCADE"),
        nullable=False,
    ),
    Column("parent_id", String(36), ForeignKey("messages.id", ondelete="CASCADE")),
    Column("seq", Integer, nullable=False),
    Column("role", String(16), nullable=False),
    Column("created_at", String(32), nullable=False),
    Column("model", Text),
    Column("effort", Text),
    Column("token_count", Integer),
    Column("prefix_hash", String(64)),  # conversation reconstruction (T1.8)
    Column("content_digest", String(64), nullable=False),  # sha256 of the plaintext body
    Column("enc", String(8), nullable=False),
    Column("body", LargeBinary, nullable=False),  # JSON message, encrypted when enc = "age"
    Index("ix_messages_conversation_seq", "conversation_id", "seq"),
    Index("ix_messages_prefix_hash", "prefix_hash"),
)

conversation_keys = Table(
    "conversation_keys",
    KEYS_METADATA,
    Column("conversation_id", String(36), primary_key=True),
    Column("kind", String(8), nullable=False),  # "plain" now; "wrapped" when locked (M2)
    Column("key", LargeBinary, nullable=False),
    Column("created_at", String(32), nullable=False),
)
