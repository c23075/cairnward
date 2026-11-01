"""Egress report (FR-EGR-1) and audit log filters (FR-AUD-3, CLI)."""

import csv
import io
import json
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from cairnward.audit import AuditLog
from cairnward.audit.egress_report import build, parse_since, render_csv, render_json, render_text
from cairnward.cli import main
from cairnward.config.paths import Paths


@pytest.fixture
def log(tmp_path: Path) -> AuditLog:
    paths = Paths(tmp_path / "cfg", tmp_path / "data", tmp_path / "state")
    audit = AuditLog.open(paths)
    a = audit.append
    pre = a("net.connect", phase="pre", decision="allow", target="pypi.org:443",
            client="aider", run_id="run-aaaa", environment="coding")  # fmt: skip
    a("net.connect", phase="post", outcome="ok", target="pypi.org:443", client="aider",
      run_id="run-aaaa", environment="coding", pre_seq=pre.seq if pre else None,
      details={"bytes_up": 1000, "bytes_down": 250_000})  # fmt: skip
    a(
        "net.connect",
        phase="event",
        decision="deny",
        target="evil.example:443",
        client="aider",
        run_id="run-aaaa",
        environment="coding",
        details={"reason": "network = allowlist"},
    )
    a(
        "net.connect",
        phase="event",
        decision="deny",
        target="evil.example:443",
        client="aider",
        run_id="run-bbbb",
        environment="coding",
        details={"reason": "network = allowlist"},
    )
    a("model.call", phase="pre", decision="allow", target="cloud", client="open-webui",
      environment="chat")  # fmt: skip
    a("model.call", phase="post", outcome="ok", target="cloud", client="open-webui",
      environment="chat", details={"prompt_tokens": 100, "completion_tokens": 20,
                                   "conversation": "conv-1"})  # fmt: skip
    a("model.call", phase="pre", decision="allow", target="local-ollama", client="open-webui",
      environment="chat")  # fmt: skip
    a("redaction", decision="allow", target="backend:cloud", client="open-webui",
      environment="chat", details={"mode": "warn", "kinds": {"email": 2}})  # fmt: skip
    return audit


def test_report(log: AuditLog) -> None:
    rows = build(log.records(), remote_backends={"cloud"})
    by = {r.destination: r for r in rows}
    assert set(by) == {"pypi.org:443", "evil.example:443", "backend:cloud"}  # local model left out
    assert (by["pypi.org:443"].allowed, by["pypi.org:443"].bytes_down) == (1, 250_000)
    assert by["evil.example:443"].denied == 2
    assert by["evil.example:443"].deny_reasons == {"network = allowlist": 2}
    cloud = by["backend:cloud"]
    assert (cloud.prompt_tokens, cloud.completion_tokens, dict(cloud.flagged)) == (
        100,
        20,
        {"email": 2},
    )
    text = render_text(rows)
    assert "244.1 KB" in text and "denied x2: network = allowlist" in text
    assert "flagged by redaction: email x2" in text
    assert json.loads(render_json(rows))[0]["destination"]
    assert len(list(csv.DictReader(io.StringIO(render_csv(rows))))) == 3
    assert render_text([]).startswith("Nothing left")


def test_filters(log: AuditLog) -> None:
    recs = log.records()
    only_run = build(recs, remote_backends={"cloud"}, run="run-b")
    assert [(r.destination, r.denied) for r in only_run] == [("evil.example:443", 1)]
    assert {r.destination for r in build(recs, remote_backends={"cloud"}, client="open-webui")} \
        == {"backend:cloud"}  # fmt: skip
    assert build(recs, remote_backends={"cloud"}, environment="nowhere") == []
    conv = build(recs, remote_backends={"cloud"}, conversation="conv-1")
    assert [r.destination for r in conv] == ["backend:cloud"]
    future = datetime.now(UTC) + timedelta(hours=1)
    assert build(recs, remote_backends={"cloud"}, since=future) == []


def test_parse_since() -> None:
    now = datetime(2026, 11, 1, 12, tzinfo=UTC)
    assert parse_since("2h", now) == datetime(2026, 11, 1, 10, tzinfo=UTC)
    assert parse_since("7d", now) == datetime(2026, 10, 25, 12, tzinfo=UTC)
    assert parse_since("2026-10-01") == datetime(2026, 10, 1, tzinfo=UTC)


def test_cli(log: AuditLog, tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
             capsys: pytest.CaptureFixture[str]) -> None:  # fmt: skip
    log.close()
    monkeypatch.setenv("CAIRNWARD_DATA_DIR", str(tmp_path / "data"))
    monkeypatch.setenv("CAIRNWARD_STATE_DIR", str(tmp_path / "state"))
    cfg = tmp_path / "cfg"
    cfg.mkdir(exist_ok=True)
    (cfg / "backends.toml").write_text(
        '[backends.local-ollama]\ndriver = "ollama"\nurl = "http://127.0.0.1:11434"\n'
        '[backends.cloud]\ndriver = "openai_compat"\nurl = "https://x.example/v1"\n'
        'location = "hosted"\n[roles.main]\nbackend = "local-ollama"\nmodel = "m"\n'
        '[roles.fast]\nbackend = "local-ollama"\nmodel = "m"\n'
    )
    assert main(["--config-dir", str(cfg), "audit", "egress", "--json"]) == 0
    rows = json.loads(capsys.readouterr().out)
    assert {r["destination"] for r in rows} == {"pypi.org:443", "evil.example:443",
                                                "backend:cloud"}  # fmt: skip
    assert main(["--config-dir", str(cfg), "audit", "log", "--run", "run-b"]) == 0
    out = capsys.readouterr().out.strip().splitlines()
    assert len(out) == 1 and "evil.example:443" in out[0]
    assert main(["--config-dir", str(cfg), "audit", "log", "--client", "open-webui",
                 "--last", "2"]) == 0  # fmt: skip
    assert len(capsys.readouterr().out.strip().splitlines()) == 2
