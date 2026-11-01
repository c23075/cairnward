"""Provenance: what untrusted content entered a conversation since the user's last
message (spec §5.3, D46, FR-POL-4).

Shown in confirmations of user-only actions started by the model, so a request that
follows, say, a web page or a tool's output is recognisably not the user's own words.
Fed by the gateway (tool results in each chat request after the last user message) and
the MCP proxy (tool outputs it relayed); keyed by client and run.
"""

from __future__ import annotations

import threading
import time
from dataclasses import dataclass, field
from typing import Any

MAX_ITEMS = 10
STALE_S = 30 * 60


def from_messages(messages: list[dict[str, Any]]) -> tuple[str, ...]:
    """Tool and function results after the last user message, as short labels."""
    last_user = max((i for i, m in enumerate(messages) if m.get("role") == "user"), default=-1)
    names: dict[str, str] = {}
    items: list[str] = []
    for m in messages[last_user + 1 :]:
        for call in m.get("tool_calls") or []:
            if isinstance(call, dict) and isinstance(call.get("function"), dict):
                names[str(call.get("id"))] = str(call["function"].get("name", "a tool"))
        if m.get("role") in ("tool", "function"):
            name = m.get("name") or names.get(str(m.get("tool_call_id"))) or "a tool"
            items.append(f"output of {str(name)[:80]}")
    return tuple(dict.fromkeys(items))[:MAX_ITEMS]


@dataclass
class _State:
    items: list[str] = field(default_factory=list)
    conversation: str | None = None
    updated: float = field(default_factory=time.monotonic)


class ProvenanceTracker:
    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._by: dict[tuple[str, str | None], _State] = {}

    def note_request(self, client: str, run_id: str | None,
                     messages: list[dict[str, Any]]) -> tuple[str, ...]:  # fmt: skip
        found = from_messages(messages)
        with self._lock:
            state = self._by.setdefault((client, run_id), _State())
            if messages and messages[-1].get("role") == "user":
                state.items = []  # the user spoke last: a fresh start
            state.items = list(dict.fromkeys([*state.items, *found]))[-MAX_ITEMS:]
            state.updated = time.monotonic()
            return tuple(state.items)

    def note_tool(self, client: str, run_id: str | None, name: str) -> None:
        with self._lock:
            state = self._by.setdefault((client, run_id), _State())
            label = f"output of {name[:80]}"
            state.items = list(dict.fromkeys([*state.items, label]))[-MAX_ITEMS:]
            state.updated = time.monotonic()

    def note_conversation(self, client: str, run_id: str | None, conversation: str) -> None:
        with self._lock:
            self._by.setdefault((client, run_id), _State()).conversation = conversation

    def current(self, client: str, run_id: str | None) -> tuple[tuple[str, ...], str | None]:
        """(provenance items, last stored conversation) for this client or run."""
        with self._lock:
            state = self._by.get((client, run_id))
            if state is None or time.monotonic() - state.updated > STALE_S:
                return (), state.conversation if state else None
            return tuple(state.items), state.conversation
