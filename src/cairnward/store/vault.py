"""Transcript vault: conversations and messages, encrypted per conversation.

- Each conversation gets its own age key in keys.db when created with crypto-shredding
  on (spec §5.5, ADR-0006). Its title and message bodies are encrypted with that key.
- With crypto-shredding off (privacy setting `crypto_shred = "off"`), content is stored
  as plain text (`enc = "none"`); purging then deletes rows (best effort on SSDs).
- Deleting is two-step: `soft_delete` moves to the trash (restorable); `purge` destroys
  the key, deletes content and leaves a tombstone row for audit and sync.
- Locking a conversation with a passphrase (key wrapping) arrives in M2; keys.db already
  records each key's kind.
"""

from __future__ import annotations

import json
import time
import uuid
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any, Self

from sqlalchemy import Connection, Engine, delete, func, insert, select, update

from cairnward.config.paths import Paths
from cairnward.store import crypto
from cairnward.store.db import checkpoint, current_revision, head_revision, make_engine, upgrade
from cairnward.store.schema import conversation_keys, conversations, folders, messages

TRASH_DAYS = 30


class VaultError(Exception):
    pass


class NotFoundError(VaultError):
    pass


class PurgedError(VaultError):
    """The conversation was purged; its content and key no longer exist."""


class LockedError(VaultError):
    """The conversation is locked with a passphrase (M2)."""


def _now() -> str:
    return datetime.now(UTC).isoformat(timespec="milliseconds")


def _new_id() -> str:
    return str(uuid.uuid4())


def _canonical(body: dict[str, Any]) -> bytes:
    return json.dumps(body, sort_keys=True, separators=(",", ":"), ensure_ascii=False).encode()


@dataclass(frozen=True)
class Conversation:
    id: str
    created_at: str
    updated_at: str
    title: str | None
    environment: str | None
    client: str | None
    folder_id: str | None
    encrypted: bool
    pinned: bool
    archived: bool
    deleted_at: str | None
    purged_at: str | None
    locked: bool = False


@dataclass(frozen=True)
class Message:
    id: str
    conversation_id: str
    parent_id: str | None
    seq: int
    role: str
    created_at: str
    body: dict[str, Any]
    model: str | None
    effort: str | None
    token_count: int | None
    prefix_hash: str | None


@dataclass(frozen=True)
class Folder:
    id: str
    name: str
    parent_id: str | None


class Vault:
    """Open with `Vault.open(paths)`; use as a context manager or call `close()`."""

    def __init__(self, vault_engine: Engine, keys_engine: Engine, data_dir: Path | None = None):
        self._db = vault_engine
        self._keys = keys_engine
        self._data_dir = data_dir  # for the conversation's artifacts (purged with it)
        # Unlocked keys of locked conversations, in memory only: id -> (key, expires)
        self._unlocked: dict[str, tuple[bytes, float]] = {}

    @classmethod
    def open(cls, paths: Paths) -> Self:
        vault_engine = make_engine(paths.data_dir / "vault.db")
        keys_engine = make_engine(paths.data_dir / "keys.db")
        upgrade(vault_engine, "vault")
        upgrade(keys_engine, "keys")
        return cls(vault_engine, keys_engine, paths.data_dir)

    @classmethod
    def files(cls, paths: Paths) -> dict[str, Path]:
        return {"vault": paths.data_dir / "vault.db", "keys": paths.data_dir / "keys.db"}

    def close(self) -> None:
        self._db.dispose()
        self._keys.dispose()

    def __enter__(self) -> Self:
        return self

    def __exit__(self, *exc: object) -> None:
        self.close()

    # ------------------------------------------------------------------ keys

    def _key(self, conversation_id: str) -> bytes:
        with self._keys.connect() as conn:
            row = conn.execute(
                select(conversation_keys.c.kind, conversation_keys.c.key).where(
                    conversation_keys.c.conversation_id == conversation_id
                )
            ).first()
        if row is None:
            raise PurgedError(f"no key for conversation {conversation_id}")
        if row.kind == "wrapped":
            held = self._unlocked.get(conversation_id)
            if held is None or held[1] < time.monotonic():
                self._unlocked.pop(conversation_id, None)
                raise LockedError(f"conversation {conversation_id} is locked")
            return held[0]
        return bytes(row.key)

    def _key_row(self, conversation_id: str) -> Any:
        with self._keys.connect() as conn:
            return conn.execute(
                select(conversation_keys.c.kind, conversation_keys.c.key).where(
                    conversation_keys.c.conversation_id == conversation_id
                )
            ).first()

    def is_locked(self, conversation_id: str) -> bool:
        row = self._key_row(conversation_id)
        return row is not None and row.kind == "wrapped"

    def lock(self, conversation_id: str, secret: str) -> None:
        """Wrap the conversation's key with a passphrase (spec §5.5, FR-ENC-1)."""
        if len(secret) < 8:
            raise VaultError("use a passphrase of at least 8 characters")
        with self._db.connect() as conn:
            row = self._row(conn, conversation_id)
        if row.purged_at is not None:
            raise PurgedError(f"conversation {conversation_id} was purged")
        if row.enc != "age":
            raise VaultError("this conversation was stored without encryption "
                             "(crypto_shred = off), so it can't be locked")  # fmt: skip
        key_row = self._key_row(conversation_id)
        if key_row is None:
            raise PurgedError(f"no key for conversation {conversation_id}")
        if key_row.kind == "wrapped":
            raise VaultError("already locked")
        wrapped = crypto.wrap_key(bytes(key_row.key), secret)
        with self._keys.begin() as kconn:
            kconn.execute(
                update(conversation_keys)
                .where(conversation_keys.c.conversation_id == conversation_id)
                .values(kind="wrapped", key=wrapped)
            )
        checkpoint(self._keys)  # the plain key is overwritten in the file too
        self._unlocked.pop(conversation_id, None)

    def unlock(self, conversation_id: str, secret: str, *, minutes: float = 30,
               permanently: bool = False) -> None:  # fmt: skip
        """Open a locked conversation for `minutes` (in memory), or remove the lock."""
        key_row = self._key_row(conversation_id)
        if key_row is None or key_row.kind != "wrapped":
            raise VaultError("not locked")
        try:
            key = crypto.unwrap_key(bytes(key_row.key), secret)
        except crypto.DecryptionError:
            raise LockedError("wrong passphrase") from None
        if permanently:
            with self._keys.begin() as kconn:
                kconn.execute(
                    update(conversation_keys)
                    .where(conversation_keys.c.conversation_id == conversation_id)
                    .values(kind="plain", key=key)
                )
            self._unlocked.pop(conversation_id, None)
            return
        self._unlocked[conversation_id] = (key, time.monotonic() + minutes * 60)

    def readable(self, conversation_id: str) -> bool:
        """False for a locked conversation that isn't unlocked right now."""
        try:
            self._key(conversation_id)
        except LockedError:
            return False
        return True

    def relock(self, conversation_id: str | None = None) -> None:
        """Forget unlocked keys (one conversation, or all)."""
        if conversation_id is None:
            self._unlocked.clear()
        else:
            self._unlocked.pop(conversation_id, None)

    def _seal(self, conv: Any, plaintext: bytes) -> bytes:
        return crypto.seal(self._key(conv.id), plaintext) if conv.enc == "age" else plaintext

    def _open(self, conv_id: str, enc: str, data: bytes) -> bytes:
        return crypto.open_sealed(self._key(conv_id), data) if enc == "age" else data

    # ------------------------------------------------------------------ conversations

    @contextmanager
    def _tx(self) -> Iterator[Connection]:
        with self._db.begin() as conn:
            yield conn

    def _row(self, conn: Connection, conversation_id: str) -> Any:
        row = conn.execute(
            select(conversations).where(conversations.c.id == conversation_id)
        ).first()
        if row is None:
            raise NotFoundError(f"conversation {conversation_id} not found")
        return row

    def create_conversation(
        self,
        *,
        environment: str | None = None,
        client: str | None = None,
        title: str | None = None,
        folder_id: str | None = None,
        crypto_shred: bool = True,
    ) -> str:
        conv_id, now = _new_id(), _now()
        enc = "age" if crypto_shred else "none"
        if crypto_shred:
            # Key first: a conversation row without its key would be unreadable.
            with self._keys.begin() as kconn:
                kconn.execute(
                    insert(conversation_keys).values(
                        conversation_id=conv_id, kind="plain", key=crypto.new_key(), created_at=now
                    )
                )
        with self._tx() as conn:
            conn.execute(
                insert(conversations).values(
                    id=conv_id,
                    created_at=now,
                    updated_at=now,
                    folder_id=folder_id,
                    environment=environment,
                    client=client,
                    enc=enc,
                    title=None,
                    pinned=False,
                    archived=False,
                )
            )
        if title is not None:
            self.set_title(conv_id, title)
        return conv_id

    def _to_conversation(self, row: Any) -> Conversation:
        title = None
        locked = row.enc == "age" and row.purged_at is None and self.is_locked(row.id)
        if row.title is not None and row.purged_at is None:
            try:
                title = self._open(row.id, row.enc, bytes(row.title)).decode()
            except LockedError:
                title = None  # shown as locked
        return Conversation(
            id=row.id,
            created_at=row.created_at,
            updated_at=row.updated_at,
            title=title,
            environment=row.environment,
            client=row.client,
            folder_id=row.folder_id,
            encrypted=row.enc == "age",
            pinned=bool(row.pinned),
            archived=bool(row.archived),
            deleted_at=row.deleted_at,
            purged_at=row.purged_at,
            locked=locked,
        )

    def conversation(self, conversation_id: str) -> Conversation:
        with self._db.connect() as conn:
            return self._to_conversation(self._row(conn, conversation_id))

    def list_conversations(
        self,
        *,
        folder_id: str | None = None,
        include_deleted: bool = False,
        environment: str | None = None,
        only_deleted: bool = False,
    ) -> list[Conversation]:
        query = select(conversations).where(conversations.c.purged_at.is_(None))
        if only_deleted:
            query = query.where(conversations.c.deleted_at.is_not(None))
        elif not include_deleted:
            query = query.where(conversations.c.deleted_at.is_(None))
        if environment is not None:
            query = query.where(conversations.c.environment == environment)
        if folder_id is not None:
            query = query.where(conversations.c.folder_id == folder_id)
        query = query.order_by(conversations.c.pinned.desc(), conversations.c.updated_at.desc())
        with self._db.connect() as conn:
            rows = conn.execute(query).all()
        return [self._to_conversation(r) for r in rows]

    def _live(self, conn: Connection, conversation_id: str) -> Any:
        row = self._row(conn, conversation_id)
        if row.purged_at is not None:
            raise PurgedError(f"conversation {conversation_id} was purged")
        return row

    def _touch(self, conn: Connection, conversation_id: str, **values: Any) -> None:
        conn.execute(
            update(conversations)
            .where(conversations.c.id == conversation_id)
            .values(updated_at=_now(), **values)
        )

    def set_title(self, conversation_id: str, title: str) -> None:
        with self._tx() as conn:
            row = self._live(conn, conversation_id)
            self._touch(conn, conversation_id, title=self._seal(row, title.encode()))

    def set_pinned(self, conversation_id: str, pinned: bool) -> None:
        with self._tx() as conn:
            self._live(conn, conversation_id)
            self._touch(conn, conversation_id, pinned=pinned)

    def set_archived(self, conversation_id: str, archived: bool) -> None:
        with self._tx() as conn:
            self._live(conn, conversation_id)
            self._touch(conn, conversation_id, archived=archived)

    def move_to_folder(self, conversation_id: str, folder_id: str | None) -> None:
        with self._tx() as conn:
            self._live(conn, conversation_id)
            if (
                folder_id is not None
                and conn.execute(select(folders.c.id).where(folders.c.id == folder_id)).first()
                is None
            ):
                raise NotFoundError(f"folder {folder_id} not found")
            self._touch(conn, conversation_id, folder_id=folder_id)

    # ------------------------------------------------------------------ messages

    def add_message(
        self,
        conversation_id: str,
        body: dict[str, Any],
        *,
        parent_id: str | None = None,
        model: str | None = None,
        effort: str | None = None,
        token_count: int | None = None,
        prefix_hash: str | None = None,
    ) -> str:
        """Store one message. `body` is the full message object (role, content, tool calls...)."""
        role = body.get("role")
        if not isinstance(role, str) or not role:
            raise VaultError("message body needs a 'role'")
        plaintext = _canonical(body)
        msg_id = _new_id()
        with self._tx() as conn:
            conv = self._live(conn, conversation_id)
            if conv.deleted_at is not None:
                raise VaultError(f"conversation {conversation_id} is in the trash")
            seq = conn.execute(
                select(func.coalesce(func.max(messages.c.seq), 0)).where(
                    messages.c.conversation_id == conversation_id
                )
            ).scalar_one()
            conn.execute(
                insert(messages).values(
                    id=msg_id,
                    conversation_id=conversation_id,
                    parent_id=parent_id,
                    seq=int(seq) + 1,
                    role=role[:16],
                    created_at=_now(),
                    model=model,
                    effort=effort,
                    token_count=token_count,
                    prefix_hash=prefix_hash,
                    content_digest=crypto.digest(plaintext),
                    enc=conv.enc,
                    body=self._seal(conv, plaintext),
                )
            )
            self._touch(conn, conversation_id)
        return msg_id

    def messages(self, conversation_id: str) -> list[Message]:
        with self._db.connect() as conn:
            self._live(conn, conversation_id)
            rows = conn.execute(
                select(messages)
                .where(messages.c.conversation_id == conversation_id)
                .order_by(messages.c.seq)
            ).all()
        out = []
        for r in rows:
            plaintext = self._open(conversation_id, r.enc, bytes(r.body))
            if crypto.digest(plaintext) != r.content_digest:
                raise VaultError(f"message {r.id} failed its integrity check")
            out.append(
                Message(
                    id=r.id,
                    conversation_id=conversation_id,
                    parent_id=r.parent_id,
                    seq=r.seq,
                    role=r.role,
                    created_at=r.created_at,
                    body=json.loads(plaintext),
                    model=r.model,
                    effort=r.effort,
                    token_count=r.token_count,
                    prefix_hash=r.prefix_hash,
                )
            )
        return out

    def find_by_prefix_hash(self, prefix_hash: str) -> tuple[str, str] | None:
        """(conversation_id, message_id) of the newest live message with this prefix hash."""
        query = (
            select(messages.c.conversation_id, messages.c.id)
            .join(conversations, conversations.c.id == messages.c.conversation_id)
            .where(
                messages.c.prefix_hash == prefix_hash,
                conversations.c.deleted_at.is_(None),
                conversations.c.purged_at.is_(None),
            )
            .order_by(messages.c.created_at.desc())
            .limit(1)
        )
        with self._db.connect() as conn:
            row = conn.execute(query).first()
        return (row.conversation_id, row.id) if row else None

    # ------------------------------------------------------------------ folders

    def create_folder(self, name: str, parent_id: str | None = None) -> str:
        if not name.strip():
            raise VaultError("folder name can't be empty")
        folder_id = _new_id()
        with self._tx() as conn:
            conn.execute(
                insert(folders).values(
                    id=folder_id, name=name.strip(), parent_id=parent_id, created_at=_now()
                )
            )
        return folder_id

    def list_folders(self) -> list[Folder]:
        with self._db.connect() as conn:
            rows = conn.execute(select(folders).order_by(folders.c.name)).all()
        return [Folder(r.id, r.name, r.parent_id) for r in rows]

    # ------------------------------------------------------------------ deletion

    def soft_delete(self, conversation_id: str) -> None:
        """Move to the trash (restorable for TRASH_DAYS)."""
        with self._tx() as conn:
            self._live(conn, conversation_id)
            self._touch(conn, conversation_id, deleted_at=_now())

    def restore(self, conversation_id: str) -> None:
        with self._tx() as conn:
            self._live(conn, conversation_id)
            self._touch(conn, conversation_id, deleted_at=None)

    def purge(self, conversation_id: str, *, force: bool = False) -> None:
        """Destroy a conversation: key first (crypto-shredding), then content.

        Leaves a tombstone row (id, created_at, purged_at) for the audit log and sync.
        Without `force`, only conversations already in the trash can be purged.
        """
        with self._db.connect() as conn:
            row = self._row(conn, conversation_id)
        if row.purged_at is not None:
            return
        if row.deleted_at is None and not force:
            raise VaultError("move the conversation to the trash first (or pass force=True)")
        # 1. Destroy the key. From here on the ciphertext anywhere (backups, SSD remnants,
        #    old copies of vault.db) can't be decrypted.
        with self._keys.begin() as kconn:
            kconn.execute(
                delete(conversation_keys).where(
                    conversation_keys.c.conversation_id == conversation_id
                )
            )
        checkpoint(self._keys)
        # 2. Delete content and reduce the row to a tombstone.
        now = _now()
        with self._tx() as conn:
            conn.execute(delete(messages).where(messages.c.conversation_id == conversation_id))
            conn.execute(
                update(conversations)
                .where(conversations.c.id == conversation_id)
                .values(
                    title=None,
                    environment=None,
                    client=None,
                    folder_id=None,
                    pinned=False,
                    archived=False,
                    deleted_at=row.deleted_at or now,
                    purged_at=now,
                    updated_at=now,
                )
            )
        checkpoint(self._db)
        # 3. The conversation's artifacts (FR-ART-1) go with it.
        if self._data_dir is not None:
            import shutil

            shutil.rmtree(self._data_dir / "artifacts" / "conversation" / conversation_id,
                          ignore_errors=True)  # fmt: skip

    def purge_expired(self, *, older_than_days: int = TRASH_DAYS) -> list[str]:
        """Purge conversations that have been in the trash longer than the limit."""
        cutoff = (datetime.now(UTC) - timedelta(days=older_than_days)).isoformat(
            timespec="milliseconds"
        )
        with self._db.connect() as conn:
            ids = [
                r.id
                for r in conn.execute(
                    select(conversations.c.id).where(
                        conversations.c.purged_at.is_(None),
                        conversations.c.deleted_at.is_not(None),
                        conversations.c.deleted_at < cutoff,
                    )
                )
            ]
        for conv_id in ids:
            self.purge(conv_id)
        return ids

    def expire(self, environment: str, days: int) -> list[str]:
        """Retention: purge this environment's conversations untouched for `days`."""
        cutoff = (datetime.now(UTC) - timedelta(days=days)).isoformat(timespec="milliseconds")
        with self._db.connect() as conn:
            ids = [
                r.id
                for r in conn.execute(
                    select(conversations.c.id).where(
                        conversations.c.purged_at.is_(None),
                        conversations.c.environment == environment,
                        conversations.c.pinned.is_(False),
                        conversations.c.updated_at < cutoff,
                    )
                )
            ]
        for conv_id in ids:
            self.purge(conv_id, force=True)
        return ids

    # ------------------------------------------------------------------ lookup and search

    def resolve_id(self, prefix: str) -> str:
        """A full conversation id from a unique prefix (as the CLI shows them)."""
        with self._db.connect() as conn:
            ids = [
                r.id
                for r in conn.execute(
                    select(conversations.c.id).where(
                        conversations.c.id.startswith(prefix, autoescape=True),
                        conversations.c.purged_at.is_(None),
                    )
                )
            ]
        if not ids:
            raise NotFoundError(f"no conversation starts with '{prefix}'")
        if len(ids) > 1:
            raise VaultError(f"'{prefix}' matches {len(ids)} conversations; give more of the id")
        return str(ids[0])

    def search(
        self, text: str, *, environment: str | None = None, limit: int = 50
    ) -> list[tuple[Conversation, str]]:
        """Conversations whose title or messages contain `text` (case-insensitive), with a
        snippet. Decrypts as it goes: no plaintext search index is kept on disk."""
        from cairnward.redaction import message_texts

        needle = text.lower()
        found: list[tuple[Conversation, str]] = []
        for conv in self.list_conversations(environment=environment):
            if conv.locked and not self.readable(conv.id):
                continue  # locked conversations aren't searched
            if conv.title and needle in conv.title.lower():
                found.append((conv, conv.title))
            else:
                for msg in self.messages(conv.id):
                    hit = next((t for t in message_texts([msg.body]) if needle in t.lower()), None)
                    if hit is not None:
                        i = hit.lower().index(needle)
                        snippet = hit[max(0, i - 40) : i + len(text) + 40].replace("\n", " ")
                        found.append((conv, snippet))
                        break
            if len(found) >= limit:
                break
        return found

    # ------------------------------------------------------------------ info

    def schema_status(self) -> dict[str, tuple[str | None, str | None]]:
        """{'vault': (current, head), 'keys': (current, head)}"""
        return {
            "vault": (current_revision(self._db), head_revision("vault")),
            "keys": (current_revision(self._keys), head_revision("keys")),
        }

    def stats(self) -> dict[str, int]:
        with self._db.connect() as conn:

            def count(query: Any) -> int:
                return int(conn.execute(query).scalar_one())

            return {
                "conversations": count(
                    select(func.count())
                    .select_from(conversations)
                    .where(
                        conversations.c.purged_at.is_(None), conversations.c.deleted_at.is_(None)
                    )
                ),
                "in_trash": count(
                    select(func.count())
                    .select_from(conversations)
                    .where(
                        conversations.c.purged_at.is_(None), conversations.c.deleted_at.is_not(None)
                    )
                ),
                "purged": count(
                    select(func.count())
                    .select_from(conversations)
                    .where(conversations.c.purged_at.is_not(None))
                ),
                "messages": count(select(func.count()).select_from(messages)),
                "folders": count(select(func.count()).select_from(folders)),
            }
