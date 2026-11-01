"""Vault CLI, search and retention (spec §4.5, FR-VLT-4; CLI side of FR-VLT-3)."""

import json
import sys
from pathlib import Path

import pytest
from sqlalchemy import update

from cairnward.audit import AuditLog
from cairnward.cli import main
from cairnward.config import load_config
from cairnward.config.paths import Paths
from cairnward.store import Vault
from cairnward.store.schema import conversations
from cairnward.vault_cli import apply_retention


@pytest.fixture
def paths(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Paths:
    p = Paths(tmp_path / "cfg", tmp_path / "data", tmp_path / "state")
    (p.config_dir / "environments").mkdir(parents=True)
    (p.config_dir / "environments" / "short.toml").write_text("vault_retention_days = 7\n")
    monkeypatch.setenv("CAIRNWARD_DATA_DIR", str(p.data_dir))
    monkeypatch.setenv("CAIRNWARD_STATE_DIR", str(p.state_dir))
    return p


def seed(paths: Paths) -> dict[str, str]:
    with Vault.open(paths) as v:
        a = v.create_conversation(environment="coding", client="aider", title="Fix the parser")
        v.add_message(a, {"role": "user", "content": "The tokenizer drops UNICODE quotes"})
        v.add_message(a, {"role": "assistant", "content": "Patch attached."}, model="qwen3:30b")
        b = v.create_conversation(environment="short", client="jan", title="Lunch ideas")
        v.add_message(b, {"role": "user", "content": [{"type": "text", "text": "noodles?"}]})
        c = v.create_conversation(environment="short", client="jan", title="Pinned plan")
        v.set_pinned(c, True)
    return {"a": a, "b": b, "c": c}


def cw(paths: Paths, *args: str) -> list[str]:
    return ["--config-dir", str(paths.config_dir), "vault", *args]


def test_list_search_show_export(paths: Paths, capsys: pytest.CaptureFixture[str],
                                 tmp_path: Path) -> None:  # fmt: skip
    ids = seed(paths)
    assert main(cw(paths)) == 0
    out = capsys.readouterr().out
    assert "Fix the parser" in out and "Lunch ideas" in out
    assert main(cw(paths, "list", "--env", "coding")) == 0
    assert "Lunch" not in capsys.readouterr().out
    assert main(cw(paths, "search", "unicode")) == 0
    out = capsys.readouterr().out
    assert ids["a"][:10] in out and "UNICODE quotes" in out and "Lunch" not in out
    assert main(cw(paths, "search", "noodles")) == 0
    assert ids["b"][:10] in capsys.readouterr().out
    assert main(cw(paths, "show", ids["a"][:8])) == 0
    md = capsys.readouterr().out
    assert md.startswith("# Fix the parser") and "## assistant (qwen3:30b)" in md
    target = tmp_path / "out.json"
    assert main(cw(paths, "export", ids["a"][:8], "--format", "json", "-o", str(target))) == 0
    data = json.loads(target.read_text())
    assert data["messages"][0]["body"]["content"].startswith("The tokenizer")
    if sys.platform != "win32":
        assert oct(target.stat().st_mode)[-3:] == "600"
    with AuditLog.open(paths) as log:
        assert [r.action for r in log.records()] == ["vault.export"]


def test_delete_restore_purge(paths: Paths, capsys: pytest.CaptureFixture[str]) -> None:
    ids = seed(paths)
    short = ids["a"][:8]
    assert main(cw(paths, "purge", short)) == 1  # not in the trash
    assert "trash first" in capsys.readouterr().out
    assert main(cw(paths, "delete", short)) == 0
    assert main(cw(paths, "list", "--trash")) == 0
    assert ids["a"][:10] in capsys.readouterr().out
    assert main(cw(paths, "restore", short)) == 0
    assert main(cw(paths, "purge", short, "--force")) == 0
    assert main(cw(paths, "show", short)) == 1
    assert "no conversation starts with" in capsys.readouterr().out
    with AuditLog.open(paths) as log:
        actions = [r.action for r in log.records()]
    assert actions == ["vault.delete", "vault.restore", "vault.purge"]


def test_retention(paths: Paths) -> None:
    ids = seed(paths)
    cfg = load_config(paths)
    with Vault.open(paths) as v:
        assert apply_retention(cfg, v) == {"short": [], "trash": []}
        with v._db.begin() as conn:  # make everything 10 days old
            conn.execute(update(conversations).values(updated_at="2000-01-01T00:00:00.000+00:00"))
        result = apply_retention(cfg, v)
        assert result["short"] == [ids["b"]]  # pinned and other environments kept
        remaining = {c.id for c in v.list_conversations()}
    assert remaining == {ids["a"], ids["c"]}
    with AuditLog.open(paths) as log:
        rec = log.records()[-1]
    assert rec.action == "vault.retention" and rec.details == {"short": 1, "trash": 0}


def test_ambiguous_prefix(paths: Paths, capsys: pytest.CaptureFixture[str]) -> None:
    seed(paths)
    assert main(cw(paths, "show", "")) == 1
    assert "matches 3 conversations" in capsys.readouterr().out
