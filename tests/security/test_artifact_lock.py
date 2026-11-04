"""Locking a conversation locks its artifacts (FR-ENC-1): real git, real age encryption."""

import shutil
from pathlib import Path

import pytest

from cairnward.artifacts import SEALED, ArtifactError, ArtifactLockedError, ArtifactStore
from cairnward.cli import main
from cairnward.config.paths import Paths
from cairnward.store import Vault

pytestmark = pytest.mark.skipif(shutil.which("git") is None, reason="needs git")

SECRET = "correct horse battery staple 42"
MARKER = b"the-secret-plan"
NAME = "hidden-file-name.md"
COMMIT = "commit-message-marker"


@pytest.fixture
def paths(tmp_path: Path) -> Paths:
    return Paths(tmp_path / "cfg", tmp_path / "data", tmp_path / "state")


def everything_under(folder: Path) -> list[Path]:
    return [p for p in folder.rglob("*") if p.is_file()]


def assert_no_plaintext(repo: Path) -> None:
    """Not the content, a file name, or a commit message, in any form git could store."""
    assert [p.name for p in everything_under(repo)] == [SEALED]
    data = (repo / SEALED).read_bytes()
    for marker in (MARKER, NAME.encode(), COMMIT.encode(), b".git"):
        assert marker not in data


def with_artifact(vault: Vault, paths: Paths) -> tuple[str, ArtifactStore]:
    cid = vault.create_conversation(title="t")
    store = ArtifactStore(paths.data_dir, keys=vault)
    store.write("conversation", cid, NAME, MARKER, COMMIT)
    return cid, store


def test_locking_seals_content_names_and_history(paths: Paths) -> None:
    with Vault.open(paths) as v:
        cid, store = with_artifact(v, paths)
        store.write("conversation", cid, NAME, MARKER + b" v2", COMMIT)
        v.lock(cid, SECRET)
        assert_no_plaintext(store.repo("conversation", cid))
        for call in (lambda: store.files("conversation", cid),
                     lambda: store.read("conversation", cid, NAME),
                     lambda: store.log("conversation", cid),
                     lambda: store.write("conversation", cid, "new.md", "x")):  # fmt: skip
            with pytest.raises(ArtifactLockedError, match="unlock it first"):
                call()
        assert_no_plaintext(store.repo("conversation", cid))


def test_a_store_without_the_vault_cant_open_it(paths: Paths) -> None:
    with Vault.open(paths) as v:
        cid, _ = with_artifact(v, paths)
        v.lock(cid, SECRET)
        v.unlock(cid, SECRET, minutes=5)  # open in the vault, but this store has no keys
        with pytest.raises(ArtifactLockedError):
            ArtifactStore(paths.data_dir).read("conversation", cid, NAME)


def test_unlocked_it_works_and_stays_sealed(paths: Paths, tmp_path: Path,
                                            monkeypatch: pytest.MonkeyPatch) -> None:  # fmt: skip
    runtime = tmp_path / "runtime"
    runtime.mkdir()
    monkeypatch.setenv("XDG_RUNTIME_DIR", str(runtime))
    with Vault.open(paths) as v:
        cid, store = with_artifact(v, paths)
        v.lock(cid, SECRET)
        v.unlock(cid, SECRET, minutes=5)
        assert store.files("conversation", cid) == [NAME]
        assert store.read("conversation", cid, NAME) == MARKER
        first = store.log("conversation", cid)[0].rev
        store.write("conversation", cid, NAME, b"second version")
        store.write("conversation", cid, "other.md", b"more")
        assert store.files("conversation", cid) == [NAME, "other.md"]
        assert len(store.log("conversation", cid)) == 3
        store.restore("conversation", cid, NAME, first)
        assert store.read("conversation", cid, NAME) == MARKER
        assert store.read("conversation", cid, NAME, store.log("conversation", cid)[2].rev) == (
            b"second version"
        )
        assert_no_plaintext(store.repo("conversation", cid))
        assert list(runtime.iterdir()) == []  # no temporary copy left behind
        v.relock(cid)
        with pytest.raises(ArtifactLockedError):
            store.read("conversation", cid, NAME)


def test_a_failed_operation_leaves_no_copy_and_changes_nothing(
    paths: Paths, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    runtime = tmp_path / "runtime"
    runtime.mkdir()
    monkeypatch.setenv("XDG_RUNTIME_DIR", str(runtime))
    with Vault.open(paths) as v:
        cid, store = with_artifact(v, paths)
        v.lock(cid, SECRET)
        v.unlock(cid, SECRET, minutes=5)
        before = (store.repo("conversation", cid) / SEALED).read_bytes()
        with pytest.raises(ArtifactError, match="no artifact"):
            store.read("conversation", cid, "missing.md")
        assert list(runtime.iterdir()) == []
        assert (store.repo("conversation", cid) / SEALED).read_bytes() == before


def test_first_artifact_of_a_locked_conversation_is_sealed_from_the_start(paths: Paths) -> None:
    with Vault.open(paths) as v:
        cid = v.create_conversation(title="t")
        v.lock(cid, SECRET)
        store = ArtifactStore(paths.data_dir, keys=v)
        with pytest.raises(ArtifactLockedError):
            store.write("conversation", cid, NAME, MARKER, COMMIT)
        assert not store.repo("conversation", cid).exists()
        v.unlock(cid, SECRET, minutes=5)
        store.write("conversation", cid, NAME, MARKER, COMMIT)
        assert_no_plaintext(store.repo("conversation", cid))
        assert store.read("conversation", cid, NAME) == MARKER


def test_removing_the_lock_brings_back_the_repository(paths: Paths) -> None:
    with Vault.open(paths) as v:
        cid, store = with_artifact(v, paths)
        v.lock(cid, SECRET)
        v.unlock(cid, SECRET, permanently=True)
        repo = store.repo("conversation", cid)
        assert not (repo / SEALED).exists() and (repo / ".git").exists()
        assert (repo / NAME).read_bytes() == MARKER
        assert ArtifactStore(paths.data_dir).read("conversation", cid, NAME) == MARKER
        assert store.log("conversation", cid)[0].message == COMMIT


def test_the_recovery_key_opens_the_artifacts(paths: Paths) -> None:
    with Vault.open(paths) as v:
        recovery = v.create_recovery_key()
        cid, store = with_artifact(v, paths)
        v.lock(cid, SECRET)
        new = "a brand new passphrase"
        v.recover(cid, recovery, new)
        v.unlock(cid, new, minutes=5)
        assert store.read("conversation", cid, NAME) == MARKER


def test_purge_destroys_the_key_so_a_copy_of_the_bundle_is_useless(paths: Paths) -> None:
    from cairnward.store import crypto

    with Vault.open(paths) as v:
        cid, store = with_artifact(v, paths)
        key = v.artifact_key(cid)
        assert key is not None
        v.lock(cid, SECRET)
        repo = store.repo("conversation", cid)
        copy = (repo / SEALED).read_bytes()  # e.g. in an old backup
        assert MARKER not in copy
        assert crypto.open_sealed(key, copy)  # that was the key, and only it opens the copy
        v.purge(cid, force=True)
        assert not repo.exists() and v.artifact_key(cid) is None


def test_locked_before_this_feature_existed(paths: Paths) -> None:
    """A plaintext repository under a locked conversation: closed to everyone with the
    vault, and sealed the next time the conversation is unlocked."""
    with Vault.open(paths) as v:
        cid = v.create_conversation(title="t")
        v.lock(cid, SECRET)
        ArtifactStore(paths.data_dir).write("conversation", cid, NAME, MARKER, COMMIT)
        store = ArtifactStore(paths.data_dir, keys=v)
        repo = store.repo("conversation", cid)
        assert (repo / ".git").exists()
        with pytest.raises(ArtifactLockedError):
            store.read("conversation", cid, NAME)
        assert store.repos() == [("conversation", cid, -1)]
        v.unlock(cid, SECRET, minutes=5)
        assert_no_plaintext(repo)
        assert store.read("conversation", cid, NAME) == MARKER


def test_crash_between_sealing_and_removing_the_plaintext(paths: Paths) -> None:
    with Vault.open(paths) as v:
        cid, store = with_artifact(v, paths)
        key = v.artifact_key(cid)
        assert key is not None
        repo = store.repo("conversation", cid)
        from cairnward.artifacts import _pack, _write_atomic
        from cairnward.store import crypto

        _write_atomic(repo / SEALED, crypto.seal(key, _pack(repo)))  # the crash came here
        assert store.read("conversation", cid, NAME) == MARKER  # with the key still plain
        assert_no_plaintext(repo)
        v.lock(cid, SECRET)  # and locking still finishes the job
        assert_no_plaintext(repo)


def test_crash_between_removing_the_lock_and_unsealing(paths: Paths) -> None:
    with Vault.open(paths) as v:
        cid, store = with_artifact(v, paths)
        key = v.artifact_key(cid)
        assert key is not None
        store.seal(cid, key)  # sealed, but the key is plain: reads and writes still work
        store.write("conversation", cid, "b.md", "b")
        assert store.files("conversation", cid) == ["b.md", NAME]


def test_a_tampered_bundle_is_an_error_not_a_crash(paths: Paths) -> None:
    with Vault.open(paths) as v:
        cid, store = with_artifact(v, paths)
        v.lock(cid, SECRET)
        v.unlock(cid, SECRET, minutes=5)
        bundle = store.repo("conversation", cid) / SEALED
        data = bytearray(bundle.read_bytes())
        data[-1] ^= 1
        bundle.write_bytes(bytes(data))
        with pytest.raises(ArtifactLockedError, match="can't decrypt"):
            store.read("conversation", cid, NAME)


def test_other_repositories_and_conversations_are_untouched(paths: Paths) -> None:
    with Vault.open(paths) as v:
        cid, store = with_artifact(v, paths)
        other, _ = with_artifact(v, paths)
        store.write("job", "job1", "result.md", "public")
        v.lock(cid, SECRET)
        assert store.read("job", "job1", "result.md") == b"public"
        assert store.read("conversation", other, NAME) == MARKER
        assert sorted(store.repos()) == sorted(
            [("conversation", cid, -1), ("conversation", other, 1), ("job", "job1", 1)]
        )


def test_cli_says_locked(paths: Paths, monkeypatch: pytest.MonkeyPatch,
                         capsys: pytest.CaptureFixture[str]) -> None:  # fmt: skip
    monkeypatch.setenv("CAIRNWARD_DATA_DIR", str(paths.data_dir))
    monkeypatch.setenv("CAIRNWARD_STATE_DIR", str(paths.state_dir))
    paths.config_dir.mkdir(parents=True, exist_ok=True)
    with Vault.open(paths) as v:
        cid, _ = with_artifact(v, paths)
        v.lock(cid, SECRET)
    base = ["--config-dir", str(paths.config_dir), "artifacts"]
    assert main([*base, "list"]) == 0
    assert "locked" in capsys.readouterr().out
    assert main([*base, "files", "conversation", cid]) == 1
    err = capsys.readouterr().err
    assert "locked" in err and NAME not in err
