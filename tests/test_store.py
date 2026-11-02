import shutil
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import pytest
from alembic.autogenerate import compare_metadata
from alembic.runtime.migration import MigrationContext
from sqlalchemy import text

from cairnward.config.paths import Paths
from cairnward.store import LockedError, NotFoundError, PurgedError, Vault, VaultError
from cairnward.store.db import METADATA, make_engine

MARKER = "SECRET-RESEARCH-NOTE-7f3a"


@pytest.fixture
def paths(tmp_path: Path) -> Paths:
    return Paths(tmp_path / "cfg", tmp_path / "data", tmp_path / "state")


@pytest.fixture
def vault(paths: Paths) -> Iterator[Vault]:
    v = Vault.open(paths)
    yield v
    v.close()


def raw_bytes(directory: Path, prefix: str) -> bytes:
    """Everything SQLite has on disk for a database: main file, WAL, journal."""
    return b"".join(p.read_bytes() for p in sorted(directory.glob(f"{prefix}*")) if p.is_file())


def checkpoint_all(v: Vault) -> None:
    for engine in (v._db, v._keys):
        with engine.connect() as conn:
            conn.execute(text("PRAGMA wal_checkpoint(TRUNCATE)"))


# ------------------------------------------------------------------ schema and settings


def test_new_databases_are_migrated_and_match_the_schema(vault: Vault) -> None:
    for name, (current, head) in vault.schema_status().items():
        assert current == head, name
    for name, engine in (("vault", vault._db), ("keys", vault._keys)):
        with engine.connect() as conn:
            diff = compare_metadata(MigrationContext.configure(conn), METADATA[name])  # type: ignore[index]
        assert diff == [], f"{name}: schema.py and migrations disagree: {diff}"


def test_sqlite_settings(vault: Vault) -> None:
    with vault._db.connect() as conn:
        assert conn.execute(text("PRAGMA journal_mode")).scalar() == "wal"
        assert conn.execute(text("PRAGMA synchronous")).scalar() == 2  # FULL
        assert conn.execute(text("PRAGMA foreign_keys")).scalar() == 1
        assert conn.execute(text("PRAGMA secure_delete")).scalar() == 1


def test_reopen_keeps_data(paths: Paths) -> None:
    with Vault.open(paths) as v:
        cid = v.create_conversation(title="kept")
        v.add_message(cid, {"role": "user", "content": "hi"})
    with Vault.open(paths) as v:
        assert v.conversation(cid).title == "kept"
        assert [m.body["content"] for m in v.messages(cid)] == ["hi"]


# ------------------------------------------------------------------ conversations and messages


def test_messages_round_trip_in_order(vault: Vault) -> None:
    cid = vault.create_conversation(environment="coding", client="aider", title="Fix tests")
    bodies: list[dict[str, Any]] = [
        {"role": "system", "content": "You are helpful."},
        {"role": "user", "content": "Ünïcödé ✓ and 中文"},
        {
            "role": "assistant",
            "content": None,
            "tool_calls": [
                {
                    "id": "1",
                    "type": "function",
                    "function": {"name": "fs.read", "arguments": '{"path": "a.py"}'},
                }
            ],
        },
    ]
    ids: list[str] = []
    for b in bodies:
        ids.append(vault.add_message(cid, b, parent_id=ids[-1] if ids else None, model="qwen3:30b"))
    got = vault.messages(cid)
    assert [m.body for m in got] == bodies
    assert [m.seq for m in got] == [1, 2, 3]
    assert got[2].parent_id == ids[1]
    conv = vault.conversation(cid)
    assert (conv.title, conv.environment, conv.client, conv.encrypted) == (
        "Fix tests",
        "coding",
        "aider",
        True,
    )


def test_message_needs_role(vault: Vault) -> None:
    cid = vault.create_conversation()
    with pytest.raises(VaultError, match="role"):
        vault.add_message(cid, {"content": "x"})


def test_unknown_conversation(vault: Vault) -> None:
    with pytest.raises(NotFoundError):
        vault.conversation("nope")


def test_list_order_pinned_first_and_folders(vault: Vault) -> None:
    a = vault.create_conversation(title="a")
    b = vault.create_conversation(title="b")
    vault.set_pinned(a, True)
    assert [c.id for c in vault.list_conversations()][:2] == [a, b]
    folder = vault.create_folder("Thesis")
    vault.move_to_folder(b, folder)
    assert [c.id for c in vault.list_conversations(folder_id=folder)] == [b]
    assert [f.name for f in vault.list_folders()] == ["Thesis"]
    with pytest.raises(NotFoundError):
        vault.move_to_folder(a, "missing")


# ------------------------------------------------------------------ encryption at rest


def test_content_and_title_are_encrypted_on_disk(vault: Vault, paths: Paths) -> None:
    cid = vault.create_conversation(title=f"title {MARKER}")
    vault.add_message(cid, {"role": "user", "content": f"body {MARKER}"})
    checkpoint_all(vault)
    assert MARKER.encode() not in raw_bytes(paths.data_dir, "vault.db")


def test_crypto_shred_off_stores_plain_text(vault: Vault, paths: Paths) -> None:
    cid = vault.create_conversation(title="plain", crypto_shred=False)
    vault.add_message(cid, {"role": "user", "content": MARKER})
    checkpoint_all(vault)
    assert not vault.conversation(cid).encrypted
    assert MARKER.encode() in raw_bytes(paths.data_dir, "vault.db")
    assert vault.messages(cid)[0].body["content"] == MARKER


def test_tampered_message_fails_integrity_check(vault: Vault) -> None:
    cid = vault.create_conversation()
    vault.add_message(cid, {"role": "user", "content": "x"})
    with vault._db.begin() as conn:
        conn.execute(text("UPDATE messages SET content_digest = :d"), {"d": "0" * 64})
    with pytest.raises(VaultError, match="integrity"):
        vault.messages(cid)


def test_locked_key_is_not_used(vault: Vault) -> None:
    cid = vault.create_conversation()
    with vault._keys.begin() as conn:
        conn.execute(text("UPDATE conversation_keys SET kind = 'wrapped'"))
    with pytest.raises(LockedError):
        vault.add_message(cid, {"role": "user", "content": "x"})


# ------------------------------------------------------------------ deletion


def test_trash_and_restore(vault: Vault) -> None:
    cid = vault.create_conversation(title="t")
    vault.soft_delete(cid)
    assert vault.list_conversations() == []
    assert [c.id for c in vault.list_conversations(include_deleted=True)] == [cid]
    with pytest.raises(VaultError, match="trash"):
        vault.add_message(cid, {"role": "user", "content": "x"})
    vault.restore(cid)
    assert [c.id for c in vault.list_conversations()] == [cid]


def test_purge_needs_trash_unless_forced(vault: Vault) -> None:
    cid = vault.create_conversation()
    with pytest.raises(VaultError, match="trash first"):
        vault.purge(cid)
    vault.purge(cid, force=True)
    assert vault.conversation(cid).purged_at is not None


def test_purge_leaves_only_a_tombstone(vault: Vault) -> None:
    cid = vault.create_conversation(title="gone", environment="coding")
    vault.add_message(cid, {"role": "user", "content": "x"})
    vault.soft_delete(cid)
    vault.purge(cid)
    vault.purge(cid)  # idempotent
    conv = vault.conversation(cid)
    assert conv.purged_at and conv.title is None and conv.environment is None
    with pytest.raises(PurgedError):
        vault.messages(cid)
    assert vault.list_conversations(include_deleted=True) == []
    assert vault.stats()["purged"] == 1 and vault.stats()["messages"] == 0


def test_crypto_shredding_defeats_old_copies(vault: Vault, paths: Paths, tmp_path: Path) -> None:
    """A copy of vault.db taken before purging (a backup, SSD remnants) is unreadable after."""
    cid = vault.create_conversation(title="sensitive")
    vault.add_message(cid, {"role": "user", "content": MARKER})
    checkpoint_all(vault)
    key = vault._key(cid)
    snapshot = tmp_path / "old-copy"
    snapshot.mkdir()
    for f in paths.data_dir.glob("vault.db*"):
        shutil.copy(f, snapshot / f.name)

    vault.soft_delete(cid)
    vault.purge(cid)

    # The key is gone from keys.db, including its WAL.
    assert key not in raw_bytes(paths.data_dir, "keys.db")
    # The old copy still has the ciphertext, but nothing can decrypt it any more.
    old = Vault(make_engine(snapshot / "vault.db"), vault._keys)
    with pytest.raises(PurgedError):
        old.messages(cid)
    old._db.dispose()


def test_purge_expired(vault: Vault) -> None:
    old = vault.create_conversation()
    recent = vault.create_conversation()
    vault.soft_delete(old)
    vault.soft_delete(recent)
    with vault._db.begin() as conn:
        conn.execute(
            text(
                "UPDATE conversations SET deleted_at = '2000-01-01T00:00:00.000+00:00' "
                "WHERE id = :id"
            ),
            {"id": old},
        )
    assert vault.purge_expired() == [old]
    assert vault.conversation(recent).purged_at is None


def test_cli_store_info(
    paths: Paths, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    from cairnward.cli import main

    monkeypatch.setenv("CAIRNWARD_DATA_DIR", str(paths.data_dir))
    assert main(["--config-dir", str(paths.config_dir), "store", "info"]) == 0
    out = capsys.readouterr().out
    assert "vault_0001 (up to date)" in out and "keys_0002 (up to date)" in out
