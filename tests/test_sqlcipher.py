"""SQLCipher databases (ADR-0019): keyed engines, wrong keys, conversion. Uses the real driver."""

import secrets
from pathlib import Path

import pytest
from sqlalchemy import text

from cairnward.config.paths import Paths
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


# ------------------------------------------------------------------ the mode, end to end


@pytest.fixture
def home(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> tuple[list[str], Paths]:
    from cairnward.store import dbkey

    cfg = tmp_path / "cfg"
    cfg.mkdir()
    monkeypatch.setenv("CAIRNWARD_DATA_DIR", str(tmp_path / "data"))
    monkeypatch.setenv("CAIRNWARD_STATE_DIR", str(tmp_path / "state"))
    monkeypatch.delenv(dbkey.ENV_VAR, raising=False)
    dbkey._cache.clear()
    return ["--config-dir", str(cfg)], Paths(cfg, tmp_path / "data", tmp_path / "state")


def _answers(monkeypatch: pytest.MonkeyPatch, *answers: str) -> None:
    it = iter(answers)
    monkeypatch.setattr("getpass.getpass", lambda prompt="": next(it))


def test_encrypt_use_and_decrypt_through_the_cli(
    home: tuple[list[str], Paths], monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:  # fmt: skip
    from cairnward.audit import AuditLog
    from cairnward.cli import main
    from cairnward.store import Vault, dbkey

    base, paths = home
    with Vault.open(paths) as vault:
        conv = vault.create_conversation(environment="default", client="test", title="t")
        vault.add_message(conv, {"role": "user", "content": SECRET})
    with AuditLog.open(paths) as log:
        log.append("settings.snapshot", actor="user", target="note-in-the-log")
    assert (paths.data_dir / "audit.jsonl").exists()

    _answers(monkeypatch, "correct horse", "correct horse")
    assert main([*base, "store", "encrypt"]) == 0
    assert "Encrypted: vault.db, keys.db, audit.db" in capsys.readouterr().out
    for name in dbkey.DATABASES:
        raw = (paths.data_dir / name).read_bytes()
        assert db.is_encrypted(paths.data_dir / name) and b"note-in-the-log" not in raw
    assert not (paths.data_dir / "audit.jsonl").exists()  # no plaintext mirror

    # a new process: no key in memory, not a terminal, no reference -> refused
    dbkey._cache.clear()
    with pytest.raises(db.DatabaseKeyError, match="encrypted"):
        Vault.open(paths)
    # the passphrase opens it; the data and the audit chain are intact
    key = dbkey.unwrap(paths, "correct horse")
    dbkey.remember(paths, key)
    with Vault.open(paths) as vault:
        assert [m.body["content"] for m in vault.messages(conv)] == [SECRET]
    with AuditLog.open(paths) as log:
        res = log.verify()
        assert res.ok and any("encrypted" in n for n in res.notes)
        assert log.latest().action == "store.encrypted"  # type: ignore[union-attr]

    # a new passphrase keeps the same key
    _answers(monkeypatch, "correct horse", "battery staple", "battery staple")
    assert main([*base, "store", "passphrase"]) == 0
    assert dbkey.unwrap(paths, "battery staple") == key
    with pytest.raises(db.DatabaseKeyError, match="wrong passphrase"):
        dbkey.unwrap(paths, "correct horse")

    # unattended: the key by reference (a weakening, warned about)
    dbkey._cache.clear()
    (paths.config_dir / "cairnward.toml").write_text('[storage]\nkey = "env:TEST_DB_KEY"\n')
    monkeypatch.setenv("TEST_DB_KEY", key.hex())
    assert dbkey.key_for(paths, ask=False) == key
    assert dbkey.weakenings(paths) and "outside its passphrase" in dbkey.weakenings(paths)[0]
    monkeypatch.setenv("TEST_DB_KEY", "00" * 32)
    dbkey._cache.clear()
    with pytest.raises(db.DatabaseKeyError):
        Vault.open(paths)

    # handed over by serve to the sandboxed core: read once, then gone from the environment
    dbkey._cache.clear()
    monkeypatch.setenv(dbkey.ENV_VAR, key.hex())
    import os

    assert dbkey.key_for(paths) == key and dbkey.ENV_VAR not in os.environ

    assert main([*base, "store", "decrypt"]) == 0
    assert not dbkey.enabled(paths)
    assert not any(db.is_encrypted(paths.data_dir / n) for n in dbkey.DATABASES)
    assert (paths.data_dir / "audit.jsonl").exists()  # the mirror is back, for every record
    with AuditLog.open(paths) as log:
        assert log.verify().ok and log.latest().action == "store.decrypted"  # type: ignore[union-attr]


def test_an_interrupted_encryption_is_finished_not_lost(
    home: tuple[list[str], Paths],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from cairnward.audit import AuditLog
    from cairnward.cli import main
    from cairnward.store import Vault, dbkey

    base, paths = home
    Vault.open(paths).close()
    AuditLog.open(paths).close()
    key = dbkey.begin(paths, "pass phrase")  # saved, wrapped, before any database changed
    db.convert(paths.data_dir / "vault.db", key, encrypt=True)  # then a crash
    _answers(monkeypatch, "pass phrase")
    assert main([*base, "store", "encrypt"]) == 0
    assert all(db.is_encrypted(paths.data_dir / n) for n in dbkey.DATABASES)
    assert dbkey.unwrap(paths, "pass phrase") == key and not dbkey.pending_file(paths).exists()


def test_backups_read_encrypted_databases(home: tuple[list[str], Paths],
                                          tmp_path: Path) -> None:  # fmt: skip
    from cairnward import backup
    from cairnward.audit import AuditLog
    from cairnward.config import load_config
    from cairnward.store import Vault, dbkey

    _, paths = home
    with Vault.open(paths) as vault:
        conv = vault.create_conversation(environment="default", client="test", title="t")
        vault.add_message(conv, {"role": "user", "content": SECRET})
    AuditLog.open(paths).close()
    key = dbkey.begin(paths, "pass phrase")
    for n in dbkey.DATABASES:
        db.convert(paths.data_dir / n, key, encrypt=True)
    dbkey.commit(paths, key)
    cfg = load_config(paths)
    out = tmp_path / "export"
    counts = backup.stage(cfg, out)
    assert counts["conversations"] == 1
    assert db.is_encrypted(out / "audit" / "audit.db")  # the copy stays encrypted
    assert (out / "audit" / dbkey.KEY_FILE).exists() and not (
        out / "audit" / "audit.jsonl"
    ).exists()
