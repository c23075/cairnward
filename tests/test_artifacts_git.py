"""Artifacts: a git repository per conversation, job or run; one commit per write;
restore = new commit (FR-ART-1); deleted with their conversation."""

import shutil
from pathlib import Path

import pytest

from cairnward.artifacts import ArtifactError, ArtifactStore
from cairnward.config.paths import Paths
from cairnward.store import Vault

pytestmark = pytest.mark.skipif(shutil.which("git") is None, reason="needs git")


def test_versions_and_restore(tmp_path: Path) -> None:
    store = ArtifactStore(tmp_path)
    r1 = store.write("conversation", "c1", "notes/plan.md", "v1")
    assert store.write("conversation", "c1", "notes/plan.md", "v1") == r1  # unchanged: no commit
    r2 = store.write("conversation", "c1", "notes/plan.md", "v2", "second draft")
    store.write("conversation", "c1", "figure.svg", "<svg/>")
    assert store.files("conversation", "c1") == ["figure.svg", "notes/plan.md"]
    assert store.read("conversation", "c1", "notes/plan.md") == b"v2"
    assert store.read("conversation", "c1", "notes/plan.md", r1) == b"v1"
    r3 = store.restore("conversation", "c1", "notes/plan.md", r1)
    assert store.read("conversation", "c1", "notes/plan.md") == b"v1"
    history = store.log("conversation", "c1", "notes/plan.md")
    assert [v.rev for v in history] == [r3, r2, r1]  # nothing rewritten
    assert history[0].message.startswith("restore notes/plan.md to")
    assert store.repos() == [("conversation", "c1", 2)]


@pytest.mark.parametrize("bad", ["../x", "/etc/passwd", ".git/config", "a/../../b", "", "a/.gitx"])
def test_path_validation(tmp_path: Path, bad: str) -> None:
    with pytest.raises(ArtifactError):
        ArtifactStore(tmp_path).write("job", "j1", bad, "x")


def test_limits_and_owners(tmp_path: Path) -> None:
    store = ArtifactStore(tmp_path)
    with pytest.raises(ArtifactError, match="limited"):
        store.write("job", "j1", "big.bin", b"x" * (3 * 1024 * 1024))
    with pytest.raises(ArtifactError):
        store.write("job", "../escape", "a.md", "x")
    with pytest.raises(ArtifactError, match="no artifact"):
        store.read("job", "j1", "missing.md")


def test_purged_with_the_conversation(tmp_path: Path) -> None:
    paths = Paths(tmp_path / "cfg", tmp_path / "data", tmp_path / "state")
    with Vault.open(paths) as vault:
        cid = vault.create_conversation(title="t")
        store = ArtifactStore(paths.data_dir)
        store.write("conversation", cid, "a.md", "secret plan")
        vault.soft_delete(cid)
        assert store.files("conversation", cid) == ["a.md"]  # still in the trash
        vault.purge(cid)
        assert not store.repo("conversation", cid).exists()
