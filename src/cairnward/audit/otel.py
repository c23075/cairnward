"""OpenTelemetry export (FR-AUD-5): model and tool calls as OTLP traces, following the
GenAI semantic conventions, for a trace viewer on this machine (Langfuse, Phoenix,
Jaeger, an OpenTelemetry Collector). Off by default: `[telemetry] otlp_endpoint =
"http://127.0.0.1:4318"` (loopback only).

Built from the audit log, so it never holds more than the log does: models, tokens,
durations, decisions and outcomes, never prompts or answers. Each finished call becomes
one span (`chat <model>`, `embeddings <model>`, `execute_tool <tool>`); calls of the same
agent run or conversation share a trace. The exporter remembers the last record it sent
(`<state>/otel.json`), so nothing is sent twice and nothing is skipped after a restart.
"""

from __future__ import annotations

import hashlib
import json
from datetime import datetime
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit

import httpx

from cairnward import __version__
from cairnward.audit.log import AuditLog, AuditRecord

OPERATIONS = {"model.call": "chat", "tool.call": "execute_tool"}


class OtelError(Exception):
    pass


def check_endpoint(url: str) -> str:
    if (urlsplit(url).hostname or "") not in ("127.0.0.1", "localhost", "::1"):
        raise OtelError("otlp_endpoint must be on this machine (e.g. http://127.0.0.1:4318)")
    return url.rstrip("/")


def _ns(ts: str) -> int:
    return int(datetime.fromisoformat(ts).timestamp() * 1_000_000_000)


def _attr(key: str, value: Any) -> dict[str, Any]:
    if isinstance(value, bool):
        return {"key": key, "value": {"boolValue": value}}
    if isinstance(value, int):
        return {"key": key, "value": {"intValue": str(value)}}
    if isinstance(value, float):
        return {"key": key, "value": {"doubleValue": value}}
    return {"key": key, "value": {"stringValue": str(value)}}


def span(post: AuditRecord, pre: AuditRecord | None) -> dict[str, Any] | None:
    """One OTLP span for a finished call (its post record, with its pre record)."""
    op = OPERATIONS.get(post.action)
    if op is None or post.phase != "post":
        return None
    d, p = post.details, (pre.details if pre else {})
    if op == "chat" and (p.get("operation") == "embeddings"
                         or str(p.get("virtual_model", "")).endswith(":embed")):  # fmt: skip
        op = "embeddings"
    model = p.get("model") or p.get("virtual_model")
    name = f"{op} {model or post.target}" if op != "execute_tool" else f"{op} {post.target}"
    end = _ns(post.ts)
    start = end - int(float(d.get("duration_s") or 0) * 1e9)
    group = post.run_id or post.conversation_id or d.get("conversation") or f"seq{post.seq}"
    attrs = [_attr("gen_ai.operation.name", op), _attr("cairnward.audit.seq", post.seq)]
    if op == "execute_tool":
        attrs.append(_attr("gen_ai.tool.name", post.target))
    else:
        attrs.append(_attr("gen_ai.provider.name", post.target or "unknown"))
        if model:
            attrs.append(_attr("gen_ai.request.model", model))
    for key, attr in (("prompt_tokens", "gen_ai.usage.input_tokens"),
                      ("completion_tokens", "gen_ai.usage.output_tokens")):  # fmt: skip
        if isinstance(d.get(key), int):
            attrs.append(_attr(attr, d[key]))
    for key, value in (("cairnward.environment", post.environment),
                       ("cairnward.client", post.client), ("cairnward.run_id", post.run_id),
                       ("cairnward.decision", pre.decision if pre else None),
                       ("cairnward.queued_s", d.get("queued_s"))):  # fmt: skip
        if value is not None:
            attrs.append(_attr(key, value))
    error = post.outcome != "ok"
    return {
        "traceId": hashlib.sha256(str(group).encode()).hexdigest()[:32],
        "spanId": hashlib.sha256(f"span{post.seq}".encode()).hexdigest()[:16],
        "name": name, "kind": 3,  # SPAN_KIND_CLIENT
        "startTimeUnixNano": str(start), "endTimeUnixNano": str(end),
        "attributes": attrs,
        "status": {"code": 2, "message": str(d.get("error", ""))[:200]} if error else {"code": 1},
    }  # fmt: skip


def payload(spans: list[dict[str, Any]]) -> dict[str, Any]:
    return {"resourceSpans": [{
        "resource": {"attributes": [_attr("service.name", "cairnward"),
                                    _attr("service.version", __version__)]},
        "scopeSpans": [{"scope": {"name": "cairnward.audit", "version": __version__},
                        "spans": spans}],
    }]}  # fmt: skip


class Exporter:
    def __init__(self, endpoint: str, state_file: Path, *,
                 transport: httpx.BaseTransport | None = None) -> None:  # fmt: skip
        self.endpoint = check_endpoint(endpoint)
        self.state_file = state_file
        self._client = httpx.Client(transport=transport, trust_env=False, timeout=10)

    def close(self) -> None:
        self._client.close()

    def _last(self) -> int:
        try:
            return int(json.loads(self.state_file.read_text(encoding="utf-8"))["seq"])
        except (OSError, ValueError, KeyError):
            return 0

    def export_once(self, log: AuditLog) -> int:
        """Send what's new; returns the number of spans sent."""
        last = self._last()
        records = log.since(last)
        if not records:
            return 0
        spans = [s for r in records
                 if (s := span(r, log.at(r.pre_seq) if r.pre_seq else None))]  # fmt: skip
        if spans:
            try:
                resp = self._client.post(f"{self.endpoint}/v1/traces", json=payload(spans))
            except httpx.HTTPError as exc:
                raise OtelError(f"the OTLP endpoint didn't answer: {exc}") from None
            if resp.status_code >= 300:
                raise OtelError(f"the OTLP endpoint answered {resp.status_code}")
        self.state_file.parent.mkdir(parents=True, exist_ok=True)
        self.state_file.write_text(json.dumps({"seq": records[-1].seq}), encoding="utf-8")
        return len(spans)
