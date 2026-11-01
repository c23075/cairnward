"""Conversation reconstruction from stateless requests (FR-VLT-2), as a property test.

Clients send the whole history every time. For any sequence of new chats, continuations
and edits of earlier turns (branches), the vault must hold every exchange so that it can
be rebuilt exactly by following parent links, and a continuation must land in the same
conversation as the exchange it continues."""

import tempfile
from pathlib import Path
from typing import Any

from hypothesis import HealthCheck, given, settings
from hypothesis import strategies as st

from cairnward.config.paths import Paths
from cairnward.gateway.recorder import normalise, record_exchange
from cairnward.store import Vault

# an operation: (kind, which earlier exchange, how far back to cut for an edit, text)
op = st.tuples(
    st.sampled_from(["new", "continue", "edit"]),
    st.integers(0, 50),
    st.integers(0, 6),
    st.text(min_size=1, max_size=12),
)
ops = st.lists(op, min_size=1, max_size=10)


def path_to(vault: Vault, conv_id: str, message_id: str) -> list[dict[str, Any]]:
    by_id = {m.id: m for m in vault.messages(conv_id)}
    out = []
    cur: str | None = message_id
    while cur is not None:
        m = by_id[cur]
        out.append(m.body)
        cur = m.parent_id
    return list(reversed(out))


@settings(max_examples=40, deadline=None, suppress_health_check=[HealthCheck.too_slow])
@given(ops)
def test_every_exchange_can_be_rebuilt(operations: list[tuple[str, int, int, str]]) -> None:
    with tempfile.TemporaryDirectory() as tmp:
        paths = Paths(Path(tmp) / "c", Path(tmp) / "d", Path(tmp) / "s")
        with Vault.open(paths) as vault:
            done: list[tuple[list[dict[str, Any]], str, str]] = []  # (full history, conv, last msg)
            for i, (kind, pick, cut, text) in enumerate(operations):
                user = {"role": "user", "content": f"{i}:{text}"}
                if kind == "new" or not done:
                    request = [{"role": "system", "content": "be brief"}, user]
                    parent_conv = None
                else:
                    history, parent_conv, _ = done[pick % len(done)]
                    if kind == "continue":
                        request = [*history, user]
                    else:  # edit an earlier user turn: keep a prefix, then a new message
                        keep = max(1, len(history) - 1 - 2 * cut)
                        request = [*history[:keep], user]
                reply = {"role": "assistant", "content": f"reply {i}"}
                rec = record_exchange(vault, request, reply, environment="e", client="c",
                                      model="m", effort="low", crypto_shred=True)  # fmt: skip
                assert rec is not None
                if kind == "continue" and parent_conv is not None:
                    assert rec.conversation_id == parent_conv
                last = max(vault.messages(rec.conversation_id), key=lambda m: m.seq)
                done.append(([*request, reply], rec.conversation_id, last.id))
            for history, conv, last_id in done:
                rebuilt = path_to(vault, conv, last_id)
                assert [normalise(m) for m in rebuilt] == [normalise(m) for m in history]
