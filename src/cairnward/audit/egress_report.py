"""Egress report: what left this machine, per destination (spec §4.9, FR-EGR-1).

Built from the audit log, so it covers exactly what Cairnward saw:
- network connections through cairnward-egress (tools, agents, MCP servers, the core's
  calls to hosted backends): allowed and denied, bytes each way;
- model calls to non-local backends: requests and tokens;
- redaction findings per destination (kinds and counts).
Filter by run, client, environment, conversation (model calls) and time.
"""

from __future__ import annotations

import csv
import io
import json
import re
from collections import Counter
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from typing import Any, Literal

from cairnward.audit.log import AuditRecord

Kind = Literal["network", "model"]


@dataclass
class Row:
    destination: str
    kind: Kind
    allowed: int = 0
    denied: int = 0
    bytes_up: int = 0
    bytes_down: int = 0
    prompt_tokens: int = 0
    completion_tokens: int = 0
    flagged: Counter[str] = field(default_factory=Counter)
    deny_reasons: Counter[str] = field(default_factory=Counter)
    clients: set[str] = field(default_factory=set)
    runs: set[str] = field(default_factory=set)
    first: str = ""
    last: str = ""

    def as_dict(self) -> dict[str, Any]:
        return {
            "destination": self.destination, "kind": self.kind, "allowed": self.allowed,
            "denied": self.denied, "bytes_up": self.bytes_up, "bytes_down": self.bytes_down,
            "prompt_tokens": self.prompt_tokens, "completion_tokens": self.completion_tokens,
            "flagged": dict(self.flagged), "deny_reasons": dict(self.deny_reasons),
            "clients": sorted(self.clients), "runs": sorted(self.runs),
            "first": self.first, "last": self.last,
        }  # fmt: skip


def parse_since(value: str, now: datetime | None = None) -> datetime:
    """'90m', '24h', '7d' (ago) or an ISO date/time."""
    now = now or datetime.now(UTC)
    m = re.fullmatch(r"(\d+)([mhd])", value.strip())
    if m:
        n, unit = int(m.group(1)), m.group(2)
        return now - {"m": timedelta(minutes=n), "h": timedelta(hours=n),
                      "d": timedelta(days=n)}[unit]  # fmt: skip
    dt = datetime.fromisoformat(value)
    return dt if dt.tzinfo else dt.replace(tzinfo=UTC)


def record_time(rec: AuditRecord) -> datetime:
    dt = datetime.fromisoformat(rec.ts.replace("Z", "+00:00"))
    return dt if dt.tzinfo else dt.replace(tzinfo=UTC)


def build(
    records: list[AuditRecord],
    *,
    remote_backends: set[str],
    run: str | None = None,
    client: str | None = None,
    environment: str | None = None,
    conversation: str | None = None,
    since: datetime | None = None,
) -> list[Row]:
    rows: dict[tuple[str, Kind], Row] = {}

    def keep(r: AuditRecord) -> bool:
        if run and not (r.run_id or "").startswith(run):
            return False
        if client and r.client != client:
            return False
        if environment and r.environment != environment:
            return False
        if since and record_time(r) < since:
            return False
        if conversation:
            conv = r.conversation_id or r.details.get("conversation") or ""
            return str(conv).startswith(conversation)
        return True

    def row(dest: str, kind: Kind, r: AuditRecord) -> Row:
        item = rows.setdefault((dest, kind), Row(dest, kind))
        item.first = item.first or r.ts
        item.last = r.ts
        if r.client:
            item.clients.add(r.client)
        if r.run_id:
            item.runs.add(r.run_id)
        return item

    for r in records:
        if not r.target or not keep(r):
            continue
        if r.action == "net.connect":
            item = row(r.target, "network", r)
            if r.phase == "pre":
                item.allowed += 1
            elif r.phase == "post":
                item.bytes_up += int(r.details.get("bytes_up", 0) or 0)
                item.bytes_down += int(r.details.get("bytes_down", 0) or 0)
            elif r.decision == "deny":
                item.denied += 1
                item.deny_reasons[str(r.details.get("reason", ""))[:120]] += 1
        elif r.action == "model.call" and r.target in remote_backends:
            item = row(f"backend:{r.target}", "model", r)
            if r.phase == "pre":
                item.allowed += 1
            elif r.phase == "post":
                item.prompt_tokens += int(r.details.get("prompt_tokens", 0) or 0)
                item.completion_tokens += int(r.details.get("completion_tokens", 0) or 0)
            elif r.decision == "deny":
                item.denied += 1
                item.deny_reasons[str(r.details.get("reason", ""))[:120]] += 1
        elif r.action == "redaction" and r.target.startswith("backend:"):
            name = r.target.removeprefix("backend:")
            if name in remote_backends:
                row(r.target, "model", r).flagged.update(r.details.get("kinds", {}))
    return sorted(rows.values(), key=lambda x: (-(x.allowed + x.denied), x.destination))


def _size(n: int) -> str:
    size = float(n)
    for unit in ("B", "KB", "MB", "GB"):
        if size < 1024 or unit == "GB":
            return f"{size:.0f} {unit}" if unit == "B" else f"{size:.1f} {unit}"
        size /= 1024
    return f"{size:.1f} GB"


def render_text(rows: list[Row]) -> str:
    if not rows:
        return "Nothing left this machine through Cairnward in this selection.\n"
    out = [f"{'DESTINATION':40} {'KIND':8} {'ALLOWED':>7} {'DENIED':>6} {'SENT':>9} "
           f"{'RECEIVED':>9} {'TOKENS':>9}  LAST"]  # fmt: skip
    for r in rows:
        tokens = r.prompt_tokens + r.completion_tokens
        out.append(
            f"{r.destination[:40]:40} {r.kind:8} {r.allowed:>7} {r.denied:>6} "
            f"{_size(r.bytes_up) if r.kind == 'network' else '-':>9} "
            f"{_size(r.bytes_down) if r.kind == 'network' else '-':>9} "
            f"{tokens if r.kind == 'model' else '-':>9}  {r.last[:19]}"
        )
        for reason, n in r.deny_reasons.most_common(3):
            out.append(f"    denied x{n}: {reason}")
        if r.flagged:
            out.append("    flagged by redaction: "
                       + ", ".join(f"{k} x{n}" for k, n in sorted(r.flagged.items())))  # fmt: skip
    return "\n".join(out) + "\n"


def render_json(rows: list[Row]) -> str:
    return json.dumps([r.as_dict() for r in rows], indent=2) + "\n"


def render_csv(rows: list[Row]) -> str:
    buf = io.StringIO()
    fields = ["destination", "kind", "allowed", "denied", "bytes_up", "bytes_down",
              "prompt_tokens", "completion_tokens", "clients", "runs", "first", "last"]  # fmt: skip
    w = csv.DictWriter(buf, fieldnames=fields, extrasaction="ignore")
    w.writeheader()
    for r in rows:
        d = r.as_dict()
        d["clients"], d["runs"] = " ".join(d["clients"]), " ".join(d["runs"])
        w.writerow(d)
    return buf.getvalue()
