"""Locking a finished job or a run's artifacts with a passphrase (FR-ENC-1)."""

import json
import shutil
from pathlib import Path

import pytest

from cairnward import vault_cli
from cairnward.artifacts import (
    SEALED,
    ArtifactError,
    ArtifactLockedError,
    ArtifactStore,
    lock_item,
    unlock_item,
)
from cairnward.cli import main
from cairnward.config.paths import Paths
from cairnward.store import Vault
from cairnward.store.vault import LockedError, VaultError

pytestmark = pytest.mark.skipif(shutil.which("git") is None, reason="needs git")

SECRET = "correct horse battery staple 42"
JOB = "abc123abc123"
QUESTION = "a-private-review-question"
RESULT = "a-private-result-text"


@pytest.fixture
def paths(tmp_path: Path) -> Paths:
    return Paths(tmp_path / "cfg", tmp_path / "data", tmp_path / "state")


def finished_job(paths: Paths, status: str = "done") -> Path:
    folder = paths.data_dir / "jobs" / JOB
    folder.mkdir(parents=True)
    (folder / "job.json").write_text(json.dumps({
        "id": JOB, "kind": "review", "params": {"question": QUESTION, "library": "thesis"},
        "steps": [{"name": "notes", "params": {}, "status": "done",
                   "output": {"findings": RESULT}}],
        "status": status, "created": "2026-11-03T00:00:00+00:00",
        "finished": "2026-11-03T01:00:00+00:00"}))  # fmt: skip
    (folder / "result.md").write_text(RESULT)
    (folder / "references.bib").write_text("@article{x, title={" + RESULT + "}}")
    ArtifactStore(paths.data_dir).write("job", JOB, "result.md", RESULT)
    return folder


def all_bytes(folder: Path) -> bytes:
    return b"".join(p.read_bytes() for p in folder.rglob("*") if p.is_file())


def test_locking_a_job_leaves_only_a_stub(paths: Paths) -> None:
    folder = finished_job(paths)
    with Vault.open(paths) as v:
        lock_item(v, paths.data_dir, "job", JOB, SECRET)
        repo = ArtifactStore(paths.data_dir).repo("job", JOB)
        for where in (folder, repo):
            data = all_bytes(where)
            assert QUESTION.encode() not in data and RESULT.encode() not in data
        assert {p.name for p in folder.iterdir()} == {SEALED, "job.json"}
        assert [p.name for p in repo.iterdir()] == [SEALED]
        store = ArtifactStore(paths.data_dir, keys=v)
        assert store.repos() == [("job", JOB, -1)]
        with pytest.raises(ArtifactLockedError, match="unlock job"):
            store.read("job", JOB, "result.md")
        with pytest.raises(ArtifactLockedError):
            store.write("job", JOB, "x.md", "x")
    job = json.loads((folder / "job.json").read_text())  # still listed
    assert job["status"] == "done" and job["steps"] == [] and job["params"] == {"locked": True}
    assert not (folder / "result.md").exists()


def test_unlock_restores_everything(paths: Paths) -> None:
    folder = finished_job(paths)
    with Vault.open(paths) as v:
        lock_item(v, paths.data_dir, "job", JOB, SECRET)
        with pytest.raises(LockedError, match="wrong passphrase"):
            unlock_item(v, paths.data_dir, "job", JOB, "not the passphrase")
        assert (folder / SEALED).exists()  # a wrong passphrase changes nothing
        unlock_item(v, paths.data_dir, "job", JOB, SECRET)
        assert not (folder / SEALED).exists()
        assert (folder / "result.md").read_text() == RESULT
        assert json.loads((folder / "job.json").read_text())["params"]["question"] == QUESTION
        assert ArtifactStore(paths.data_dir).read("job", JOB, "result.md").decode() == RESULT
        with pytest.raises(VaultError, match="not locked"):
            unlock_item(v, paths.data_dir, "job", JOB, SECRET)
        lock_item(v, paths.data_dir, "job", JOB, "a different passphrase")  # can lock again


@pytest.mark.parametrize("status", ["queued", "running"])
def test_an_unfinished_job_cant_be_locked(paths: Paths, status: str) -> None:
    folder = finished_job(paths, status)
    with Vault.open(paths) as v:
        with pytest.raises(ArtifactError, match="hasn't finished"):
            lock_item(v, paths.data_dir, "job", JOB, SECRET)
        assert (folder / "result.md").exists() and not v.is_locked(f"job:{JOB}")


def test_refusals(paths: Paths) -> None:
    with Vault.open(paths) as v:
        with pytest.raises(ArtifactError, match="no job"):
            lock_item(v, paths.data_dir, "job", JOB, SECRET)
        with pytest.raises(ArtifactError, match="no artifacts"):
            lock_item(v, paths.data_dir, "run", "run1", SECRET)
        with pytest.raises(ArtifactError, match="vault lock"):
            lock_item(v, paths.data_dir, "conversation", "c1", SECRET)
        finished_job(paths)
        with pytest.raises(VaultError, match="at least 8"):
            lock_item(v, paths.data_dir, "job", JOB, "short")
        lock_item(v, paths.data_dir, "job", JOB, SECRET)
        with pytest.raises(VaultError, match="already locked"):
            lock_item(v, paths.data_dir, "job", JOB, SECRET)


def test_a_run_repository_and_the_recovery_key(paths: Paths) -> None:
    store = ArtifactStore(paths.data_dir)
    store.write("run", "run1", "out.md", RESULT)
    with Vault.open(paths) as v:
        recovery = v.create_recovery_key()
        lock_item(v, paths.data_dir, "run", "run1", SECRET)
        assert RESULT.encode() not in all_bytes(store.repo("run", "run1"))
        with pytest.raises(ArtifactLockedError):
            store.files("run", "run1")
        new = "a brand new passphrase"
        v.recover("run:run1", recovery, new)
        with pytest.raises(LockedError):
            unlock_item(v, paths.data_dir, "run", "run1", SECRET)
        unlock_item(v, paths.data_dir, "run", "run1", new)
        assert store.read("run", "run1", "out.md").decode() == RESULT


def test_crash_after_the_key_was_saved_can_be_undone(paths: Paths) -> None:
    folder = finished_job(paths)
    with Vault.open(paths) as v:
        v.lock_item(f"job:{JOB}", SECRET)  # the crash came here: nothing sealed yet
        unlock_item(v, paths.data_dir, "job", JOB, SECRET)
        assert (folder / "result.md").read_text() == RESULT
        assert not v.is_locked(f"job:{JOB}")


def test_cli(paths: Paths, monkeypatch: pytest.MonkeyPatch,
             capsys: pytest.CaptureFixture[str]) -> None:  # fmt: skip
    monkeypatch.setenv("CAIRNWARD_DATA_DIR", str(paths.data_dir))
    monkeypatch.setenv("CAIRNWARD_STATE_DIR", str(paths.state_dir))
    paths.config_dir.mkdir(parents=True, exist_ok=True)
    monkeypatch.setattr(vault_cli, "ask_passphrase", lambda prompt: SECRET)
    finished_job(paths)
    base = ["--config-dir", str(paths.config_dir), "artifacts"]
    assert main([*base, "lock", "job", JOB]) == 0
    assert main([*base, "lock", "job", JOB]) == 1
    assert main([*base, "show", "job", JOB, "result.md"]) == 1
    assert main([*base, "unlock", "job", JOB]) == 0
    out = capsys.readouterr()
    assert SECRET not in out.out + out.err and "already locked" in out.err
    assert main([*base, "show", "job", JOB, "result.md"]) == 0
    assert capsys.readouterr().out == RESULT
    audit = (paths.data_dir / "audit.jsonl").read_text()
    assert '"artifacts.lock"' in audit and SECRET not in audit
