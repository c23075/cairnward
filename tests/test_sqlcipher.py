"""SQLCipher databases (ADR-0019): keyed engines, wrong keys, conversion. Uses the real driver."""

import secrets
from pathlib import Path

import pytest
from sqlalchemy import text

from cairnward.store import db

sqlcipher3 = pytest.importorskip("sqlcipher3", reason="SQLCipher driver (Linux x86-64 only)")

SECRET = "a message body nobody should find on disk"


def _fill(path: Path, key: bytes | None) -> None:
    engine = db.make_engine(path, key)
    with engine.begin() as conn:
        conn.execute(text("CREATE TABLE notes (id INTEGER PRIMARY KEY, body TEXT)"))
        conn.execute(text("INSERT INTO notes (body) VALUES (:b)"), {"b": SECRET})
    db.checkpoint(engine)
    engine.dispose()


def _bodies(path: Path, key: bytes | None) -> list[str]:
    engine = db.make_engine(path, key)
    with engine.connect() as conn:
        rows = [str(r[0]) for r in conn.execute(text("SELECT body FROM notes"))]
    engine.dispose()
    return rows


def test_an_encrypted_database_hides_everything_on_disk(tmp_path: Path) -> None:
    key = secrets.token_bytes(db.KEY_BYTES)
    path = tmp_path / "vault.db"
    _fill(path, key)
    raw = path.read_bytes()
    assert db.is_encrypted(path)
    assert b"SQLite format 3" not in raw[:16]
    assert SECRET.encode() not in raw and b"notes" not in raw  # content and schema
    assert _bodies(path, key) == [SECRET]


def test_the_wrong_key_or_no_key_is_refused(tmp_path: Path) -> None:
    key = secrets.token_bytes(db.KEY_BYTES)
    path = tmp_path / "keys.db"
    _fill(path, key)
    with pytest.raises(db.DatabaseKeyError, match="doesn't open"):
        db.connect(path, secrets.token_bytes(db.KEY_BYTES))
    with pytest.raises(db.DatabaseKeyError, match="is encrypted"):
        db.connect(path)
    with pytest.raises(db.DatabaseKeyError, match="32 bytes"):
        db.connect(path, b"short")
    plain = tmp_path / "plain.db"
    _fill(plain, None)
    with pytest.raises(db.DatabaseKeyError, match="doesn't open"):
        db.connect(plain, key)


def test_settings_and_migrations_work_on_an_encrypted_database(tmp_path: Path) -> None:
    key = secrets.token_bytes(db.KEY_BYTES)
    engine = db.make_engine(tmp_path / "audit.db", key)
    db.upgrade(engine, "audit")
    with engine.connect() as conn:
        assert conn.execute(text("PRAGMA journal_mode")).scalar() == "wal"
        assert conn.execute(text("PRAGMA secure_delete")).scalar() == 1
    assert db.stored_revisions(engine) == [db.HEADS["audit"]]
    engine.dispose()


def test_convert_to_encrypted_and_back_keeps_the_data(tmp_path: Path) -> None:
    key = secrets.token_bytes(db.KEY_BYTES)
    path = tmp_path / "vault.db"
    engine = db.make_engine(path)
    db.upgrade(engine, "vault")
    engine.dispose()
    _fill(path, None)
    db.convert(path, key, encrypt=True)
    assert db.is_encrypted(path) and SECRET.encode() not in path.read_bytes()
    assert _bodies(path, key) == [SECRET]
    engine = db.make_engine(path, key)
    assert db.stored_revisions(engine) == [db.HEADS["vault"]]  # migrations carried over
    engine.dispose()
    with pytest.raises(db.DatabaseKeyError, match="already encrypted"):
        db.convert(path, key, encrypt=True)
    with pytest.raises(db.DatabaseKeyError):
        db.convert(path, secrets.token_bytes(db.KEY_BYTES), encrypt=False)
    assert _bodies(path, key) == [SECRET]  # a failed conversion leaves the file as it was
    db.convert(path, key, encrypt=False)
    assert not db.is_encrypted(path) and _bodies(path, None) == [SECRET]
    assert not (tmp_path / "vault.db.converting").exists()
