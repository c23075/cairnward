import json
import threading
from collections.abc import Iterator
from itertools import pairwise
from pathlib import Path

import pytest
from hypothesis import HealthCheck, given, settings
from hypothesis import strategies as st
from sqlalchemy import text

from cairnward.audit import GENESIS, AuditError, AuditLog, digest, enabled_for, record_settings
from cairnward.config import load_config
from cairnward.config.paths import Paths


@pytest.fixture
def paths(tmp_path: Path) -> Paths:
    return Paths(tmp_path / "cfg", tmp_path / "data", tmp_path / "state")


@pytest.fixture
def log(paths: Paths) -> Iterator[AuditLog]:
    with AuditLog.open(paths) as audit:
        yield audit


def sql(log: AuditLog, statement: str, **params: object) -> None:
    with log._engine.begin() as conn:
        conn.execute(text(statement), params)


def fill(log: AuditLog, n: int = 5) -> None:
    for i in range(n):
        log.append("tool.call", actor="agent:aider", target=f"fs.read file{i}", decision="allow")


# ------------------------------------------------------------------ writing


def test_chain_links_and_verifies(log: AuditLog) -> None:
    fill(log, 5)
    recs = log.records()
    assert [r.seq for r in recs] == [1, 2, 3, 4, 5]
    assert recs[0].prev_hash == GENESIS
    assert all(b.prev_hash == a.hash for a, b in pairwise(recs))
    res = log.verify()
    assert res.ok and res.records == 5 and (res.head_seq, res.head_hash) == log.head()


def test_mirror_has_identical_lines(log: AuditLog) -> None:
    fill(log, 3)
    lines = log.mirror.read_text().splitlines()
    assert lines == [r.to_json_line() for r in log.records()]
    assert json.loads(lines[0])["action"] == "tool.call"


def test_effect_writes_pre_and_post(log: AuditLog) -> None:
    with log.effect(
        "model.call", actor="client:open-webui", params="prompt bytes", conversation_id="c1"
    ) as eff:
        eff.result("reply bytes", tokens=42)
    pre, post = log.records()
    assert (pre.phase, post.phase, post.outcome, post.pre_seq) == ("pre", "post", "ok", pre.seq)
    assert pre.params_digest == digest("prompt bytes")
    assert post.result_digest == digest("reply bytes") and post.details == {"tokens": 42}


def test_effect_records_errors_and_reraises(log: AuditLog) -> None:
    with pytest.raises(ValueError), log.effect("tool.call", actor="agent:x"):
        raise ValueError("boom")
    post = log.records()[-1]
    assert (post.outcome, post.details) == ("error", {"error": "ValueError"})


@pytest.mark.parametrize(
    "details",
    [
        {"content": "hi"},
        {"nested": {"Prompt": "x"}},
        {"items": [{"passphrase": "p"}]},
        {"note": "x" * 600},
    ],
)
def test_content_is_rejected(log: AuditLog, details: dict[str, object]) -> None:
    with pytest.raises(AuditError):
        log.append("tool.call", details=details)
    assert log.records() == []


def test_disabled_environment_is_not_recorded(paths: Paths) -> None:
    with AuditLog.open(paths, enabled_for=lambda env: env != "scratch") as audit:
        assert audit.append("tool.call", environment="scratch") is None
        assert audit.append("tool.call", environment="coding") is not None
        assert audit.append("settings.changed", environment="scratch") is not None


def test_concurrent_writers_keep_one_chain(paths: Paths) -> None:
    logs = [AuditLog.open(paths) for _ in range(4)]

    def work(audit: AuditLog) -> None:
        for _ in range(25):
            audit.append("tool.call", actor="agent:x")

    threads = [threading.Thread(target=work, args=(lg,)) for lg in logs]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    res = logs[0].verify()
    assert res.ok, res.problems
    assert res.records == 100
    for lg in logs:
        lg.close()


def test_mirror_catches_up_after_crash(paths: Paths) -> None:
    with AuditLog.open(paths) as audit:
        fill(audit, 3)
        lines = audit.mirror.read_text().splitlines()
        audit.mirror.write_text("\n".join(lines[:1]) + "\n")  # crash lost 2 mirror lines
    with AuditLog.open(paths) as audit:  # reopening re-syncs
        assert audit.verify().ok


# ------------------------------------------------------------------ tampering


def test_edited_record_detected(log: AuditLog) -> None:
    fill(log)
    sql(log, "UPDATE records SET target = 'something innocent' WHERE seq = 3")
    res = log.verify()
    assert not res.ok
    assert any("record 3: contents changed" in p for p in res.problems)
    assert any("record 3: database and mirror disagree" in p for p in res.problems)


def test_deleted_record_detected(log: AuditLog) -> None:
    fill(log)
    sql(log, "DELETE FROM records WHERE seq = 2")
    problems = " ".join(log.verify().problems)
    assert "record 2: missing" in problems and "does not link" in problems


@pytest.mark.parametrize("suffix", [" ", "\n", "\t"])
def test_whitespace_added_to_stored_details_detected(log: AuditLog, suffix: str) -> None:
    """Found by the property test: JSON ignores trailing whitespace, verify must not."""
    fill(log)
    sql(log, "UPDATE records SET details = details || :s WHERE seq = 2", s=suffix)
    assert any("record 2: stored details were edited" in p for p in log.verify().problems)


def test_edited_mirror_detected(log: AuditLog) -> None:
    fill(log)
    lines = log.mirror.read_text().splitlines()
    lines[1] = lines[1].replace("file1", "file9")
    log.mirror.write_text("\n".join(lines) + "\n")
    assert any("record 2: database and mirror disagree" in p for p in log.verify().problems)


def test_full_rewrite_needs_a_noted_head(log: AuditLog) -> None:
    """An attacker who rewrites a record AND recomputes every later hash (and the mirror)
    produces a self-consistent chain; only a head noted earlier exposes it."""
    fill(log, 4)
    noted = log.head()
    recs = log.records()
    sql(log, "DELETE FROM records")
    log.mirror.write_text("")
    for r in recs:
        log.append(r.action, actor=r.actor, target="rewritten" if r.seq == 2 else r.target)
    assert log.verify().ok  # self-consistent forgery
    res = log.verify(expect=[noted])
    assert not res.ok and "hash differs from the expected head" in res.problems[0]


def test_truncation_needs_a_noted_head(log: AuditLog) -> None:
    fill(log, 4)
    noted = log.head()
    sql(log, "DELETE FROM records WHERE seq = 4")
    lines = log.mirror.read_text().splitlines()[:3]
    log.mirror.write_text("\n".join(lines) + "\n")
    assert log.verify().ok
    assert "is missing (log truncated or rewritten)" in log.verify(expect=[noted]).problems[0]


FIELDS = ["ts", "actor", "action", "phase", "target", "decision", "details", "prev_hash"]


@settings(max_examples=40, suppress_health_check=[HealthCheck.function_scoped_fixture])
@given(
    seq=st.integers(1, 6), column=st.sampled_from(FIELDS), suffix=st.text(min_size=1, max_size=3)
)
def test_any_field_change_is_detected(
    tmp_path_factory: pytest.TempPathFactory, seq: int, column: str, suffix: str
) -> None:
    base = tmp_path_factory.mktemp("h")
    with AuditLog.open(Paths(base, base / "d", base / "s")) as audit:
        fill(audit, 6)
        with audit._engine.begin() as conn:
            conn.execute(
                text(f"UPDATE records SET {column} = {column} || :s WHERE seq = :q"),  # noqa: S608
                {"s": suffix, "q": seq},
            )
        assert not audit.verify().ok


# ------------------------------------------------------------------ settings changes


def write_main(paths: Paths, body: str) -> None:
    paths.config_dir.mkdir(parents=True, exist_ok=True)
    (paths.config_dir / "cairnward.toml").write_text(body)


def test_settings_snapshot_then_changes(paths: Paths) -> None:
    cfg = load_config(paths)
    with AuditLog.open(paths, enabled_for(cfg)) as audit:
        first = record_settings(cfg, audit)
        assert first.first_snapshot and first.changes == []
        assert record_settings(cfg, audit).changes == []  # nothing changed, nothing written
        assert len(audit.records()) == 1

    write_main(paths, '[privacy]\napprovals = "tiered"\n')
    cfg = load_config(paths)
    with AuditLog.open(paths, enabled_for(cfg)) as audit:
        sync = record_settings(cfg, audit)
        changed = {(c["scope"], c["setting"]) for c in sync.changes if "setting" in c}
        assert ("global", "approvals") in changed
        rec = audit.latest("settings.changed")
        assert rec is not None
        assert {
            "scope": "global",
            "setting": "approvals",
            "before": "high_only",
            "after": "tiered",
        } in rec.details["changes"]
        assert audit.verify().ok


def test_audit_off_writes_marker_then_stops(paths: Paths) -> None:
    cfg = load_config(paths)
    with AuditLog.open(paths, enabled_for(cfg)) as audit:
        record_settings(cfg, audit)

    write_main(paths, '[privacy]\naudit = "off"\n')
    cfg = load_config(paths)
    with AuditLog.open(paths, enabled_for(cfg)) as audit:
        sync = record_settings(cfg, audit)
        assert sync.disabled
        assert [r.action for r in audit.records()][-1] == "audit.disabled"
        n = len(audit.records())
        assert audit.append("tool.call", environment="coding") is None  # off now
        assert record_settings(cfg, audit).disabled is False  # marker not repeated
        assert len(audit.records()) == n

    write_main(paths, "")
    cfg = load_config(paths)
    with AuditLog.open(paths, enabled_for(cfg)) as audit:
        sync = record_settings(cfg, audit)
        assert sync.enabled_after_gap_s is not None
        actions = [r.action for r in audit.records()]
        assert actions[-3:] == ["audit.enabled", "settings.changed", "settings.snapshot"]
        assert audit.verify().ok


def test_environment_named_like_a_forbidden_key(paths: Paths) -> None:
    (paths.config_dir / "environments").mkdir(parents=True)
    (paths.config_dir / "environments" / "text.toml").write_text("")
    cfg = load_config(paths)
    with AuditLog.open(paths, enabled_for(cfg)) as audit:
        assert record_settings(cfg, audit).first_snapshot


# ------------------------------------------------------------------ CLI


def test_cli_verify_head_and_log(
    paths: Paths, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    from cairnward.cli import main

    monkeypatch.setenv("CAIRNWARD_DATA_DIR", str(paths.data_dir))
    cfg_dir = ["--config-dir", str(paths.config_dir)]
    assert main([*cfg_dir, "config", "check"]) == 0
    assert "first settings snapshot recorded" in capsys.readouterr().out
    assert main([*cfg_dir, "audit", "head"]) == 0
    head = capsys.readouterr().out.strip()
    assert head.startswith("1:")
    assert main([*cfg_dir, "audit", "verify", "--expect", head]) == 0
    assert "OK: 1 records" in capsys.readouterr().out
    assert main([*cfg_dir, "audit", "log", "--json"]) == 0
    assert json.loads(capsys.readouterr().out)["action"] == "settings.snapshot"
    with AuditLog.open(paths) as audit:
        sql(audit, "UPDATE records SET actor = 'someone else'")
    assert main([*cfg_dir, "audit", "verify"]) == 1
    assert "contents changed" in capsys.readouterr().err


def test_audit_schema_matches_migrations(log: AuditLog) -> None:
    from alembic.autogenerate import compare_metadata
    from alembic.runtime.migration import MigrationContext

    from cairnward.store.db import METADATA

    with log._engine.connect() as conn:
        assert compare_metadata(MigrationContext.configure(conn), METADATA["audit"]) == []
