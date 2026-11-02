"""OpenTelemetry export (FR-AUD-5): GenAI-convention spans built from the audit log, no
content, sent once each, loopback endpoints only."""

import json
from pathlib import Path
from typing import Any

import httpx
import pytest

from cairnward.audit import AuditLog
from cairnward.audit.otel import Exporter, OtelError
from cairnward.config.paths import Paths


def test_spans_from_the_audit_log(tmp_path: Path) -> None:
    paths = Paths(tmp_path / "cfg", tmp_path / "data", tmp_path / "state")
    sent: list[dict[str, Any]] = []

    def collector(req: httpx.Request) -> httpx.Response:
        assert req.url.path == "/v1/traces"
        sent.append(json.loads(req.read()))
        return httpx.Response(200, json={})

    with AuditLog.open(paths) as log:
        pre = log.append("model.call", actor="client:jan", phase="pre", target="local-ollama",
                         environment="coding", client="jan", decision="allow",
                         details={"model": "qwen3:30b", "virtual_model": "cw/coding:medium",
                                  "secret_prompt": "never exported"}).seq  # type: ignore[union-attr]  # fmt: skip
        log.append(
            "model.call",
            actor="client:jan",
            phase="post",
            target="local-ollama",
            environment="coding",
            client="jan",
            pre_seq=pre,
            outcome="ok",
            details={"prompt_tokens": 120, "completion_tokens": 30, "duration_s": 2.5},
        )
        tpre = log.append("tool.call", actor="client:jan", phase="pre", target="fake/echo",
                          run_id="run-1").seq  # type: ignore[union-attr]  # fmt: skip
        log.append(
            "tool.call",
            actor="client:jan",
            phase="post",
            target="fake/echo",
            run_id="run-1",
            pre_seq=tpre,
            outcome="error",
            details={"error": "boom"},
        )
        log.append("settings.snapshot", actor="user")  # not a call: no span
        exp = Exporter("http://127.0.0.1:4318", paths.state_dir / "otel.json",
                       transport=httpx.MockTransport(collector))  # fmt: skip
        assert exp.export_once(log) == 2
        assert exp.export_once(log) == 0  # nothing twice
        log.append("run.end", actor="user")
        assert exp.export_once(log) == 0 and len(sent) == 1
    spans = sent[0]["resourceSpans"][0]["scopeSpans"][0]["spans"]
    chat, tool = spans
    attrs = {a["key"]: next(iter(a["value"].values())) for a in chat["attributes"]}
    assert chat["name"] == "chat qwen3:30b" and attrs["gen_ai.request.model"] == "qwen3:30b"
    assert attrs["gen_ai.usage.input_tokens"] == "120" and attrs["cairnward.client"] == "jan"
    assert int(chat["endTimeUnixNano"]) - int(chat["startTimeUnixNano"]) == 2_500_000_000
    assert tool["name"] == "execute_tool fake/echo" and tool["status"]["code"] == 2
    assert "never exported" not in json.dumps(sent)


def test_only_loopback() -> None:
    with pytest.raises(OtelError, match="on this machine"):
        Exporter("https://otel.example.com", Path("otel.json"))
