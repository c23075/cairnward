"""Table definitions for the two databases (SQLAlchemy Core).

vault.db  conversations, messages, folders. Content and titles are encrypted per
          conversation (age) unless crypto-shredding is switched off.
research.db  literature libraries: documents, page-aware chunks, keyword (FTS5) and
             vector (sqlite-vec) indexes. Vector tables are created per library because
             their dimension depends on the embedding model.
audit.db  the hash-chained audit log (metadata and digests only, never content).
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
AUDIT_METADATA = MetaData()
RESEARCH_METADATA = MetaData()

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
    # when locked and a recovery key is set up: the key, also encrypted to its recipient
    Column("recovery", LargeBinary),
)

# The optional recovery key's public half (FR-ENC-1); the secret half is shown once.
recovery_key = Table(
    "recovery_key",
    KEYS_METADATA,
    Column("id", Integer, primary_key=True),
    Column("recipient", Text, nullable=False),
    Column("created_at", String(32), nullable=False),
)

audit_records = Table(
    "records",
    AUDIT_METADATA,
    Column("seq", Integer, primary_key=True, autoincrement=False),
    Column("ts", String(32), nullable=False),
    Column("actor", Text, nullable=False),  # user | client:<name> | agent:<name> | cairnward
    Column("action", Text, nullable=False),  # e.g. model.call, tool.call, net.connect
    Column("phase", String(8), nullable=False),  # pre | post | event
    Column("target", Text),
    Column("decision", String(8)),  # allow | ask | deny | None
    Column("outcome", String(8)),  # ok | error | None (post records)
    Column("environment", Text),
    Column("client", Text),
    Column("run_id", Text),
    Column("conversation_id", Text),
    Column("pre_seq", Integer),  # post records point at their pre record
    Column("params_digest", String(64)),
    Column("result_digest", String(64)),
    Column("details", Text, nullable=False),  # canonical JSON, metadata only
    Column("prev_hash", String(64), nullable=False),
    Column("hash", String(64), nullable=False, unique=True),
    Index("ix_records_action", "action"),
    Index("ix_records_conversation", "conversation_id"),
)

libraries = Table(
    "libraries",
    RESEARCH_METADATA,
    Column("id", Integer, primary_key=True),
    Column("name", String(63), nullable=False, unique=True),
    Column("folder", Text, nullable=False),
    Column("environment", Text, nullable=False),
    Column("embedding_model", Text),  # virtual model used, e.g. cw/litreview-offline:embed
    Column("embedding_dim", Integer),
    Column("created_at", String(32), nullable=False),
)

documents = Table(
    "documents",
    RESEARCH_METADATA,
    Column("id", Integer, primary_key=True),
    Column("library_id", Integer, ForeignKey("libraries.id", ondelete="CASCADE"), nullable=False),
    Column("path", Text, nullable=False),  # relative to the library folder
    Column("sha256", String(64), nullable=False),
    Column("title", Text),
    Column("authors", Text),
    Column("year", Integer),
    Column("pages", Integer),
    Column("status", String(12), nullable=False),  # indexed | no_text | failed | missing
    Column("error", Text),
    Column("chunks", Integer, nullable=False, default=0),
    Column("indexed_at", String(32), nullable=False),
    Index("ix_documents_library_path", "library_id", "path", unique=True),
)

chunks = Table(
    "chunks",
    RESEARCH_METADATA,
    Column("id", Integer, primary_key=True),
    Column("document_id", Integer, ForeignKey("documents.id", ondelete="CASCADE"), nullable=False),
    Column("seq", Integer, nullable=False),
    Column("page_start", Integer, nullable=False),
    Column("page_end", Integer, nullable=False),
    Column("text", Text, nullable=False),
    Column("tokens", Integer, nullable=False),
    Index("ix_chunks_document_seq", "document_id", "seq"),
)
