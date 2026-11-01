"""Record gateway exchanges in the transcript vault (spec §4.5, ADR-0011).

The OpenAI API is stateless: every request carries the whole history. Each message gets
a prefix hash (hash of all messages up to and including it). A new request is matched to
the conversation holding the longest stored prefix of its history; only the new messages
and the reply are appended. A history that diverges from what's stored (an edited
message) continues from the branch point inside the same conversation.

Messages are normalised before hashing, because clients often echo the assistant's reply
back slightly differently (dropping `reasoning_content`, `null` vs missing content).
If a client changes an earlier message in other ways, its next turn starts a branch.
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from typing import Any

from cairnward.store import LockedError, Vault

TITLE_CHARS = 80


def normalise(msg: dict[str, Any]) -> dict[str, Any]:
    out: dict[str, Any] = {"role": msg.get("role")}
    content = msg.get("content")
    out["content"] = "" if content is None else content
    if msg.get("tool_calls"):
        out["tool_calls"] = [
            {"id": c.get("id"), "name": c.get("function", {}).get("name"),
             "arguments": c.get("function", {}).get("arguments")}
            for c in msg["tool_calls"]
        ]  # fmt: skip
    for key in ("tool_call_id", "name"):
        if msg.get(key):
            out[key] = msg[key]
    return out


def prefix_hashes(messages: list[dict[str, Any]]) -> list[str]:
    hashes, prev = [], ""
    for m in messages:
        canon = json.dumps(normalise(m), sort_keys=True, separators=(",", ":"), ensure_ascii=False)
        prev = hashlib.sha256(f"{prev}\n{canon}".encode()).hexdigest()
        hashes.append(prev)
    return hashes


def _title(messages: list[dict[str, Any]]) -> str | None:
    for m in messages:
        if m.get("role") == "user":
            c = m.get("content")
            text = c if isinstance(c, str) else " ".join(
                p.get("text", "") for p in c or [] if isinstance(p, dict))  # fmt: skip
            text = " ".join(text.split())
            if text:
                return text[:TITLE_CHARS] + ("…" if len(text) > TITLE_CHARS else "")
    return None


@dataclass(frozen=True)
class Recorded:
    conversation_id: str
    new_conversation: bool
    stored_messages: int


def record_exchange(
    vault: Vault,
    request_messages: list[dict[str, Any]],
    reply: dict[str, Any],
    *,
    environment: str,
    client: str | None,
    model: str,
    effort: str,
    crypto_shred: bool,
    completion_tokens: int | None = None,
) -> Recorded | None:
    """Store the request's new messages and the reply. None if the conversation is locked."""
    hashes = prefix_hashes([*request_messages, reply])
    match = None
    matched = 0
    for k in range(len(request_messages), 0, -1):
        match = vault.find_by_prefix_hash(hashes[k - 1])
        if match:
            matched = k
            break
    try:
        if match:
            conv_id, parent = match
            new = False
        else:
            conv_id = vault.create_conversation(
                environment=environment, client=client, title=_title(request_messages),
                crypto_shred=crypto_shred,
            )  # fmt: skip
            parent, new = None, True
        stored = 0
        for i in range(matched, len(request_messages)):
            parent = vault.add_message(conv_id, request_messages[i], parent_id=parent,
                                       prefix_hash=hashes[i])  # fmt: skip
            stored += 1
        vault.add_message(conv_id, reply, parent_id=parent, prefix_hash=hashes[-1], model=model,
                          effort=effort, token_count=completion_tokens)  # fmt: skip
    except LockedError:
        return None
    return Recorded(conv_id, new, stored + 1)
