import hashlib
import shutil
import subprocess
from pathlib import Path

import pytest

from cairnward.audit import AuditLog
from cairnward.config.paths import Paths
from cairnward.run.history import RunNotFound, find_run, list_runs
from cairnward.run.snapshots import MAX_FILE_BYTES, Snapshots

pytestmark = pytest.mark.skipif(shutil.which("git") is None, reason="git not installed")


@pytest.fixture
def ws(tmp_path: Path) -> Path:
    w = tmp_path / "project"
    (w / "src").mkdir(parents=True)
    (w / "src" / "app.py").write_text("def add(a, b):\n    return a - b\n")
    (w / "README.md").write_text("hello\n")
    (w / "logo.bin").write_bytes(bytes(range(256)) * 10)
    (w / ".gitignore").write_text("secret.env\n")
    (w / "secret.env").write_text("TOKEN=x\n")
    (w / "node_modules" / "pkg").mkdir(parents=True)
    (w / "node_modules" / "pkg" / "index.js").write_text("x")
    return w


def agent_edits(w: Path) -> None:
    (w / "src" / "app.py").write_text("def add(a, b):\n    return a + b\n")  # modified
    (w / "src" / "test_app.py").write_text("from app import add\n")  # added
    (w / "README.md").unlink()  # deleted
    (w / "logo.bin").write_bytes(b"\x00" * 10)  # binary modified


def test_changes_diff_and_revert(tmp_path: Path, ws: Path) -> None:
    snaps = Snapshots(tmp_path / "state", ws)
    snaps.take("r1", "before")
    agent_edits(ws)
    snaps.take("r1", "after")
    changes = {c.path: c.status for c in snaps.changes("r1")}
    assert changes == {"src/app.py": "M", "src/test_app.py": "A", "README.md": "D", "logo.bin": "M"}
    diff = snaps.diff("r1")
    assert "-    return a - b" in diff and "+    return a + b" in diff
    assert "app.py" in snaps.diff("r1", stat=True)

    result = snaps.revert("r1")
    assert sorted(result.restored) == ["README.md", "logo.bin", "src/app.py", "src/test_app.py"]
    assert result.conflicts == []
    assert (ws / "src" / "app.py").read_text().endswith("a - b\n")
    assert not (ws / "src" / "test_app.py").exists()
    assert (ws / "README.md").read_text() == "hello\n"
    assert (ws / "logo.bin").read_bytes() == bytes(range(256)) * 10  # byte-exact


def test_ignored_and_large_files_are_not_snapshotted(tmp_path: Path, ws: Path) -> None:
    (ws / "huge.dat").write_bytes(b"0" * (MAX_FILE_BYTES + 1))
    snaps = Snapshots(tmp_path / "state", ws)
    snaps.take("r1", "before")
    (ws / "secret.env").write_text("TOKEN=changed\n")
    (ws / "node_modules" / "pkg" / "index.js").write_text("y")
    (ws / "huge.dat").write_bytes(b"1" * (MAX_FILE_BYTES + 1))
    snaps.take("r1", "after")
    assert snaps.changes("r1") == []
    store = b"".join(p.read_bytes() for p in snaps.git_dir.rglob("*") if p.is_file())
    assert b"TOKEN=" not in store  # .gitignore'd secrets aren't copied into snapshots


def test_conflicts_are_left_alone_unless_forced(tmp_path: Path, ws: Path) -> None:
    snaps = Snapshots(tmp_path / "state", ws)
    snaps.take("r1", "before")
    agent_edits(ws)
    snaps.take("r1", "after")
    (ws / "src" / "app.py").write_text("# my own later edit\n")
    result = snaps.revert("r1")
    assert result.conflicts == ["src/app.py"]
    assert (ws / "src" / "app.py").read_text() == "# my own later edit\n"
    forced = snaps.revert("r1", force=True)
    assert "src/app.py" in forced.restored and (ws / "src" / "app.py").read_text().endswith(
        "a - b\n"
    )


def test_project_git_repository_is_untouched(tmp_path: Path, ws: Path) -> None:
    git = shutil.which("git")
    assert git
    subprocess.run([git, "init", "-q"], cwd=ws, check=True)

    def fingerprint() -> str:
        h = hashlib.sha256()
        for p in sorted((ws / ".git").rglob("*")):
            if p.is_file():
                h.update(str(p.relative_to(ws)).encode() + p.read_bytes())
        return h.hexdigest()

    before = fingerprint()
    snaps = Snapshots(tmp_path / "state", ws)
    snaps.take("r1", "before")
    agent_edits(ws)
    snaps.take("r1", "after")
    snaps.revert("r1")
    assert fingerprint() == before
    assert ".git/HEAD" not in [c.path for c in snaps.changes("r1")]


def test_runs_and_prune(tmp_path: Path, ws: Path) -> None:
    snaps = Snapshots(tmp_path / "state", ws)
    for rid in ("r1", "r2"):
        snaps.take(rid, "before")
        snaps.take(rid, "after")
    assert snaps.runs() == ["r1", "r2"] and snaps.has_run("r1")
    assert snaps.prune({"r2"}) == 1
    assert snaps.runs() == ["r2"] and not snaps.has_run("r1")


def test_history_and_cli(tmp_path: Path, ws: Path, monkeypatch: pytest.MonkeyPatch,
                         capsys: pytest.CaptureFixture[str]) -> None:  # fmt: skip
    from cairnward.cli import main

    paths = Paths(tmp_path / "cfg", tmp_path / "data", tmp_path / "state")
    monkeypatch.setenv("CAIRNWARD_DATA_DIR", str(paths.data_dir))
    monkeypatch.setenv("CAIRNWARD_STATE_DIR", str(paths.state_dir))
    rid = "abcdef12-0000-4000-8000-000000000000"
    snaps = Snapshots(paths.state_dir, ws)
    snaps.take(rid, "before")
    agent_edits(ws)
    snaps.take(rid, "after")
    with AuditLog.open(paths) as audit:
        audit.append("run.start", actor="user", run_id=rid, client="aider", target="aider",
                     environment="coding", details={"workspace": str(ws)})  # fmt: skip
        audit.append("run.end", actor="user", run_id=rid, client="aider",
                     details={"exit_code": 0, "duration_s": 3.0, "changed_files": 4})  # fmt: skip
        (info,) = list_runs(audit)
        assert info.changed_files == 4 and find_run(audit, "abcd").run_id == rid
        with pytest.raises(RunNotFound):
            find_run(audit, "zzz")
    base = ["--config-dir", str(paths.config_dir), "runs"]
    assert main([*base, "list"]) == 0
    assert "abcdef12" in capsys.readouterr().out
    assert main([*base, "diff", "abcdef12", "--stat"]) == 0
    assert "src/app.py" in capsys.readouterr().out
    assert main([*base, "revert", "abcdef12"]) == 0
    assert "4 file(s) restored" in capsys.readouterr().out
    assert (ws / "README.md").exists()
    with AuditLog.open(paths) as audit:
        assert audit.records()[-1].action == "run.revert" and audit.verify().ok
