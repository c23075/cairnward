"""Audit anchoring to a git repo (FR-AUD-4), with a local bare repo."""

import json
import shutil
import sqlite3
import subprocess
import sys
from pathlib import Path

import pytest

from cairnward.audit import AuditLog
from cairnward.audit.anchor import Anchorer, AnchorError
from cairnward.cli import main
from cairnward.config.models import AnchorConfig
from cairnward.config.paths import Paths

pytestmark = pytest.mark.skipif(
    shutil.which("ssh-keygen") is None or shutil.which("git") is None or sys.platform == "win32",
    reason="needs git and ssh-keygen (Linux first)",
)


@pytest.fixture
def setup(tmp_path: Path) -> tuple[Paths, AuditLog, Anchorer, Path]:
    paths = Paths(tmp_path / "cfg", tmp_path / "data", tmp_path / "state")
    remote = tmp_path / "anchors.git"
    subprocess.run(["git", "init", "--quiet", "--bare", "-b", "main", str(remote)], check=True)
    log = AuditLog.open(paths)
    log.append("settings.snapshot", actor="user")
    anchorer = Anchorer(paths, AnchorConfig(repo=remote.as_uri()))
    keys = anchorer.setup()
    assert keys["deploy"].startswith("ssh-ed25519") and keys["sign"].startswith("ssh-ed25519")
    return paths, log, anchorer, remote


def test_anchor_and_verify(setup: tuple[Paths, AuditLog, Anchorer, Path], tmp_path: Path) -> None:
    _, log, anchorer, remote = setup
    first = anchorer.anchor(log)
    assert first is not None and first["seq"] == 1
    assert anchorer.anchor(log) is None  # nothing new
    log.append("run.start", actor="user")
    assert anchorer.anchor(log)["seq"] == 2  # type: ignore[index]
    log.append("run.end", actor="user")
    res = anchorer.verify(log)
    assert res.ok and res.anchors == 2 and res.last_seq == 2 and res.unanchored == 1
    # the remote really holds them (a fresh clone)
    clone = tmp_path / "clone"
    subprocess.run(["git", "clone", "--quiet", str(remote), str(clone)], check=True)
    found = next(iter((clone / "anchors").glob("*/checkpoints.jsonl")))
    lines = found.read_text().splitlines()
    assert [json.loads(x)["payload"]["seq"] for x in lines] == [1, 2]
    assert "content" not in lines[0]


def test_tampering_is_detected(setup: tuple[Paths, AuditLog, Anchorer, Path]) -> None:
    paths, log, anchorer, _ = setup
    log.append("model.call", actor="client:x", target="local-ollama")
    anchorer.anchor(log)
    log.close()
    # someone rewrites an anchored record and recomputes nothing (or everything) locally
    con = sqlite3.connect(paths.data_dir / "audit.db")
    con.execute("UPDATE records SET hash = ? WHERE seq = 2", ("f" * 64,))
    con.commit()
    con.close()
    with AuditLog.open(paths) as reopened:
        res = anchorer.verify(reopened)
    assert not res.ok and any("differs from its anchor" in p for p in res.problems)


def test_forged_anchor_is_detected(setup: tuple[Paths, AuditLog, Anchorer, Path]) -> None:
    _, log, anchorer, _ = setup
    anchorer.anchor(log)
    _, checkpoints = anchorer._files()
    entry = json.loads(checkpoints.read_text().splitlines()[0])
    entry["payload"]["hash"] = "0" * 64  # changed without re-signing
    checkpoints.write_text(json.dumps(entry) + "\n")
    anchorer._git("commit", "--quiet", "-am", "forge")
    anchorer._git("push", "--quiet", "origin", "HEAD:main")
    res = anchorer.verify(log)
    assert not res.ok and any("bad signature" in p for p in res.problems)


def test_not_set_up(tmp_path: Path) -> None:
    paths = Paths(tmp_path / "c", tmp_path / "d", tmp_path / "s")
    with AuditLog.open(paths) as log, pytest.raises(AnchorError, match="isn't set up"):
        Anchorer(paths, AnchorConfig(repo="file:///nowhere")).anchor(log)


def test_cli(tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
             capsys: pytest.CaptureFixture[str]) -> None:  # fmt: skip
    remote = tmp_path / "anchors.git"
    subprocess.run(["git", "init", "--quiet", "--bare", "-b", "main", str(remote)], check=True)
    cfg = tmp_path / "cfg"
    cfg.mkdir()
    (cfg / "cairnward.toml").write_text(f'[anchor]\nrepo = "{remote.as_uri()}"\n')
    monkeypatch.setenv("CAIRNWARD_DATA_DIR", str(tmp_path / "data"))
    monkeypatch.setenv("CAIRNWARD_STATE_DIR", str(tmp_path / "state"))
    base = ["--config-dir", str(cfg), "audit"]
    assert main([*base, "anchor", "--setup"]) == 0
    assert "deploy key" in capsys.readouterr().out
    assert main([*base, "anchor"]) == 0
    assert "nothing to anchor yet" in capsys.readouterr().out
    with AuditLog.open(Paths(cfg, tmp_path / "data", tmp_path / "state")) as log:
        log.append("settings.snapshot", actor="user")
    assert main([*base, "anchor"]) == 0
    assert "anchored seq" in capsys.readouterr().out
    assert main([*base, "verify", "--anchors"]) == 0
    out = capsys.readouterr().out
    assert "chain and mirror intact" in out and "anchors OK: 1 checkpoint(s)" in out
