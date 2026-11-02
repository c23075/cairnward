"""The optional recovery key (FR-ENC-1): it reopens locked conversations whose passphrase
is forgotten; only its public half is stored; conversations locked before it existed are
covered once unlocked; removing it destroys every copy; neither it nor the passphrases
reach the audit log or the database files."""

from pathlib import Path

import pytest

from cairnward import vault_cli
from cairnward.cli import main
from cairnward.config.paths import Paths
from cairnward.store import Vault, crypto
from cairnward.store.vault import LockedError, VaultError

OLD = "first passphrase 1234"
NEW = "brand new passphrase 5678"


@pytest.fixture
def paths(tmp_path: Path) -> Paths:
    return Paths(tmp_path / "cfg", tmp_path / "data", tmp_path / "state")


def test_recovery_key(paths: Paths) -> None:
    with Vault.open(paths) as v:
        before = v.create_conversation(title="Locked before")
        v.add_message(before, {"role": "user", "content": "early"})
        v.lock(before, OLD)
        assert v.recovery_status() == {"set_up": False, "created_at": None, "locked": 1,
                                       "covered": 0}  # fmt: skip
        secret = v.create_recovery_key()
        assert secret.startswith("AGE-SECRET-KEY-1")
        with pytest.raises(VaultError, match="already set up"):
            v.create_recovery_key()
        after = v.create_conversation(title="Locked after")
        v.add_message(after, {"role": "user", "content": "later"})
        v.lock(after, OLD)
        assert v.recovery_status()["covered"] == 1
        # forgotten passphrase: the recovery key sets a new one; still locked
        with pytest.raises(LockedError, match="isn't a recovery key"):
            v.recover(after, "not a key", NEW)
        other, _ = crypto.new_recovery_identity()
        with pytest.raises(LockedError, match="doesn't open"):
            v.recover(after, other, NEW)
        v.recover(after, secret, NEW)
        assert v.is_locked(after) and not v.readable(after)
        with pytest.raises(LockedError):
            v.unlock(after, OLD)
        v.unlock(after, NEW, minutes=1)
        assert v.messages(after)[0].body["content"] == "later"
        # locked before the key existed: not covered until unlocked once
        with pytest.raises(VaultError, match="locked before the recovery key"):
            v.recover(before, secret, NEW)
        v.unlock(before, OLD, minutes=1)
        assert v.recovery_status()["covered"] == 2
        v.relock()
        v.recover(before, secret, NEW)
        v.unlock(before, NEW, minutes=1)
        # removing it: every copy goes; passphrases still work
        assert v.remove_recovery_key() == 2
        assert v.recovery_status() == {"set_up": False, "created_at": None, "locked": 2,
                                       "covered": 0}  # fmt: skip
        with pytest.raises(VaultError, match="locked before"):
            v.recover(after, secret, OLD)
        v.unlock(after, NEW, minutes=1)
        # unlocking for good drops the recovery copy with the lock
        v.create_recovery_key()
        v.unlock(before, NEW, minutes=1)
        v.unlock(before, NEW, permanently=True)
        assert v.recovery_status()["locked"] == 1


def test_cli_and_no_leaks(paths: Paths, monkeypatch: pytest.MonkeyPatch,
                          capsys: pytest.CaptureFixture[str]) -> None:  # fmt: skip
    monkeypatch.setenv("CAIRNWARD_DATA_DIR", str(paths.data_dir))
    monkeypatch.setenv("CAIRNWARD_STATE_DIR", str(paths.state_dir))
    paths.config_dir.mkdir(parents=True)
    base = ["--config-dir", str(paths.config_dir), "vault"]
    with Vault.open(paths) as v:
        cid = v.create_conversation(title="Medical")
        v.add_message(cid, {"role": "user", "content": "results"})
    assert main([*base, "recovery-key"]) == 0
    assert "no recovery key" in capsys.readouterr().out
    assert main([*base, "recovery-key", "create"]) == 0
    shown = capsys.readouterr().out
    secret = next(w for w in shown.split() if w.startswith("AGE-SECRET-KEY-1"))
    answers = iter([OLD, OLD, secret, NEW, NEW])
    monkeypatch.setattr(vault_cli, "ask_passphrase", lambda prompt: next(answers))
    assert main([*base, "lock", cid[:8]]) == 0
    assert "recovery key can also open it" in capsys.readouterr().out
    assert main([*base, "recover", cid[:8]]) == 0
    assert main([*base, "recovery-key", "status"]) == 0
    assert "opens 1 of 1" in capsys.readouterr().out
    with Vault.open(paths) as v:
        v.unlock(cid, NEW, minutes=1)
    for f in paths.data_dir.rglob("*"):
        if f.is_file():
            data = f.read_bytes()
            for s in (secret, OLD, NEW):
                assert s.encode() not in data, f"{s[:6]}... found in {f}"
            assert secret.upper().encode() not in data
    assert main([*base, "recovery-key", "remove"]) == 0
    assert "1 conversation copies destroyed" in capsys.readouterr().out
