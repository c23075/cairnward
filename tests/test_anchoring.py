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


@pytest.mark.skipif(shutil.which("openssl") is None, reason="needs openssl")
def test_checkpoints_get_rfc3161_timestamps(setup: tuple[Paths, AuditLog, Anchorer, Path],
                                            tmp_path: Path,
                                            monkeypatch: pytest.MonkeyPatch) -> None:  # fmt: skip
    from cairnward.audit import timestamp
    from tests.test_timestamp import authority, make_tsa

    paths, log, _anchorer, remote = setup
    tsa_dir = make_tsa(tmp_path)
    real_request = timestamp.request
    monkeypatch.setattr(timestamp, "request", lambda url, data: real_request(
        url, data, transport=authority(tsa_dir)))  # fmt: skip
    timed = Anchorer(paths, AnchorConfig(repo=remote.as_uri(), tsa_url="https://tsa.example/tsr",
                                         tsa_ca_file=str(tsa_dir / "ca.crt")))  # fmt: skip
    assert timed.anchor(log) is not None
    res = timed.verify(log)
    assert res.ok and res.timestamped == 1 and "GMT" in (res.last_tsa_time or "")
    # an authority that's down: the checkpoint is still made, the error noted
    monkeypatch.setattr(timestamp, "request", lambda url, data: (_ for _ in ()).throw(
        timestamp.TimestampError("the timestamp authority didn't answer")))  # fmt: skip
    log.append("run.start", actor="user")
    assert timed.anchor(log) is not None
    _, checkpoints = timed._files()
    assert "tsr_error" in checkpoints.read_text().splitlines()[-1]


# ------------------------------------------------------------------ Merkle checkpoints


def test_checkpoints_sign_a_merkle_root_that_extends_the_last(
    setup: tuple[Paths, AuditLog, Anchorer, Path],
) -> None:
    from cairnward.audit import merkle

    _, log, anchorer, _ = setup
    first = anchorer.anchor(log)
    assert first is not None and first["v"] == 2 and first["root"] == merkle.root(log.hashes(1))
    for i in range(6):
        log.append("run.start", actor="user", details={"n": i})
    second = anchorer.anchor(log)
    assert second is not None and second["seq"] == 7
    res = anchorer.verify(log)
    assert res.ok and res.merkle_roots == 2, res.problems


def test_a_forged_root_or_rewritten_history_is_caught(
    setup: tuple[Paths, AuditLog, Anchorer, Path],
) -> None:
    _, log, anchorer, _ = setup
    log.append("run.start", actor="user")
    assert anchorer.anchor(log) is not None
    _, checkpoints = anchorer._files()
    entry = json.loads(checkpoints.read_text().splitlines()[-1])
    entry["payload"]["root"] = "00" * 32  # the signature now fails too, so re-sign it
    text = json.dumps(entry["payload"], sort_keys=True, separators=(",", ":"))
    entry["sig"] = anchorer._sign(text)
    checkpoints.write_text(json.dumps(entry, sort_keys=True) + "\n")
    anchorer._sync = lambda: None  # type: ignore[method-assign]  # keep the local edit
    res = anchorer.verify(log)
    assert not res.ok and any("Merkle root differs" in p for p in res.problems)


def test_version_1_checkpoints_still_verify(setup: tuple[Paths, AuditLog, Anchorer, Path]) -> None:
    _, log, anchorer, _ = setup
    seq, head = log.head()
    signers, checkpoints = anchorer._files()
    signers.parent.mkdir(parents=True, exist_ok=True)
    signers.write_text(f"cairnward {anchorer.sign_key.with_suffix('.pub').read_text().strip()}\n")
    payload = {"v": 1, "install": anchorer.install_id, "seq": seq, "hash": head,
               "ts": "2026-11-01T00:00:00+00:00"}  # fmt: skip
    text = json.dumps(payload, sort_keys=True, separators=(",", ":"))
    checkpoints.write_text(json.dumps({"payload": payload, "sig": anchorer._sign(text)}) + "\n")
    anchorer._sync = lambda: None  # type: ignore[method-assign]
    res = anchorer.verify(log)
    assert res.ok and res.anchors == 1 and res.merkle_roots == 0, res.problems


def test_prove_and_check_a_record_without_the_log(
    setup: tuple[Paths, AuditLog, Anchorer, Path],
    tmp_path: Path,
) -> None:
    from cairnward.audit.anchor import AnchorError, check_proof, make_proof

    _, log, anchorer, _ = setup
    for i in range(9):
        log.append("tool.call", actor="user", details={"n": i})
    with pytest.raises(AnchorError, match="no checkpoint"):
        make_proof(anchorer, log, 4)
    assert anchorer.anchor(log) is not None
    bundle = json.loads(json.dumps(make_proof(anchorer, log, 4)))  # as written to a file
    signers, _ = anchorer._files()
    trusted = tmp_path / "trusted-signers"
    trusted.write_text(signers.read_text())
    assert check_proof(bundle, trusted) == []
    assert check_proof(bundle, None) == [
        "signature not checked: pass the signers file you trust (--signers)"
    ]
    edited = json.loads(json.dumps(bundle))
    edited["record"]["action"] = "vault.purge"
    assert any("edited" in p for p in check_proof(edited, trusted))
    moved = json.loads(json.dumps(bundle))
    moved["proof"][0] = "11" * 32
    assert any("isn't in the checkpoint" in p for p in check_proof(moved, trusted))
    other_key = tmp_path / "other"
    subprocess.run(["ssh-keygen", "-q", "-t", "ed25519", "-N", "", "-f", str(other_key)],
                   check=True)  # fmt: skip
    stranger = tmp_path / "stranger-signers"
    stranger.write_text(f"cairnward {(tmp_path / 'other.pub').read_text().strip()}\n")
    assert any("signature" in p for p in check_proof(bundle, stranger))


def test_prove_and_check_proof_cli(tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
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
    with AuditLog.open(Paths(cfg, tmp_path / "data", tmp_path / "state")) as log:
        for _ in range(3):
            log.append("settings.snapshot", actor="user")
    assert main([*base, "anchor"]) == 0
    assert main([*base, "verify", "--anchors"]) == 0
    assert "Merkle roots OK: 1" in capsys.readouterr().out
    proof = tmp_path / "proof.json"
    assert main([*base, "prove", "2", "--out", str(proof)]) == 0
    capsys.readouterr()
    assert main(["audit", "check-proof", str(proof)]) == 2  # signature not checked
    assert "signature not checked" in capsys.readouterr().out
    signers = next(p for p in tmp_path.rglob("signers") if p.is_file())
    assert main(["audit", "check-proof", str(proof), "--signers", str(signers)]) == 0
    assert "signature OK" in capsys.readouterr().out
