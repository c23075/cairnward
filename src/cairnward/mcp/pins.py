"""MCP tool pinning and change detection (spec §4.2, FR-MCP-3; v0.5 D38).

Each tool's whole definition as clients get it (the standard MCP fields: name, title,
description, input/output schema, annotations, icons, _meta) is hashed. With
`mcp_pinning = "on"`, a tool is offered only once you approved exactly that hash
(`cairnward mcp approve`); a changed definition (a "rug pull") disables it until
re-approved. Descriptions are also scanned for instruction-like text (tool poisoning),
shown when you review them. With pinning off, every tool is offered and changes are only
recorded.

The pins live in the data folder (owner-only); approving is a user-only action done
through the management socket, never by a client or model.
"""

from __future__ import annotations

import hashlib
import json
import re
import threading
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Literal

Status = Literal["approved", "new", "changed"]

# Instruction-like text that has no business in a tool description.
_SUSPICIOUS: list[tuple[str, re.Pattern[str]]] = [
    (label, re.compile(rx, re.IGNORECASE))
    for label, rx in [
        ("hidden-instruction tag", r"<\s*(important|system|instructions?|secret)\b"),
        ("tries to override instructions",
         r"\b(ignore|disregard|forget)\b.{0,40}\b(previous|prior|above|earlier|other)\b"),
        ("asks for secrecy", r"\b(do not|don't|never)\s+(tell|mention|inform|show|reveal)\b"),
        ("mentions the user being unaware", r"\b(without|before)\s+(telling|informing|asking)\b"),
        ("refers to credentials or keys",
         r"(\.ssh|id_rsa|id_ed25519|\.aws|\.netrc|api[_ -]?keys?|passwords?|private keys?)"),
        ("talks about other tools", r"\b(other|another|all)\s+tools?\b.{0,60}\b(must|should)\b"),
        ("addresses the model", r"\b(you are|as an ai|assistant must|the model must)\b"),
        ("invisible characters", r"[​-‏‪-‮⁠-⁤﻿]"),
    ]
]  # fmt: skip


def tool_hash(tool: dict[str, Any]) -> str:
    canonical = json.dumps(tool, sort_keys=True, ensure_ascii=False, separators=(",", ":"))
    return hashlib.sha256(canonical.encode()).hexdigest()


def scan(tool: dict[str, Any]) -> list[str]:
    """Warnings about instruction-like text anywhere in the definition's strings."""
    texts: list[str] = []

    def walk(value: Any) -> None:
        if isinstance(value, str):
            texts.append(value)
        elif isinstance(value, dict):
            for v in value.values():
                walk(v)
        elif isinstance(value, list):
            for v in value:
                walk(v)

    walk({k: v for k, v in tool.items() if k != "name"})
    blob = "\n".join(texts)
    flags = [label for label, rx in _SUSPICIOUS if rx.search(blob)]
    if len(tool.get("description") or "") > 4000:
        flags.append("very long description")
    return flags


@dataclass(frozen=True)
class ToolState:
    name: str
    hash: str
    status: Status
    flags: list[str]


class PinStore:
    def __init__(self, path: Path):
        self.path = path
        self._lock = threading.Lock()
        self._data: dict[str, dict[str, Any]] = {}
        if path.exists():
            self._data = json.loads(path.read_text(encoding="utf-8"))

    @classmethod
    def at(cls, data_dir: Path) -> PinStore:
        return cls(data_dir / "mcp_pins.json")

    def _save(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        tmp = self.path.with_suffix(".tmp")
        tmp.write_text(json.dumps(self._data, indent=2, sort_keys=True), encoding="utf-8")
        tmp.chmod(0o600)
        tmp.replace(self.path)

    def state(self, server: str, tool: dict[str, Any],
              raw: dict[str, Any] | None = None) -> ToolState:  # fmt: skip
        """`tool` as clients get it (standard fields); `raw` as the server sent it.

        Pins made before the MCP SDK (format 1) hashed the raw definition: one that still
        matches what the server sends now is carried over to the new hash automatically
        (the same definition the user approved); anything else counts as changed."""
        h = tool_hash(tool)
        pinned = self._data.get(server, {}).get("tools", {}).get(tool["name"])
        legacy = pinned is not None and pinned.get("format", 1) < 2 and pinned["hash"] != h
        if legacy and raw is not None and pinned is not None and pinned["hash"] == tool_hash(raw):
            with self._lock:
                pinned.update(hash=h, format=2, migrated_at=int(time.time()))
                self._save()
        if pinned is None:
            status: Status = "new"
        elif pinned["hash"] == h:
            status = "approved"
        else:
            status = "changed"
        return ToolState(tool["name"], h, status, scan(tool))

    def approve(self, server: str, approvals: dict[str, str], server_version: str = "") -> None:
        """Pin tool name -> hash (exactly the definitions the user reviewed)."""
        with self._lock:
            entry = self._data.setdefault(server, {"tools": {}})
            for name, h in approvals.items():
                entry["tools"][name] = {"hash": h, "approved_at": int(time.time()), "format": 2}
            if server_version:
                entry["server_version"] = server_version
            self._save()

    def forget(self, server: str) -> bool:
        with self._lock:
            found = self._data.pop(server, None) is not None
            if found:
                self._save()
            return found
