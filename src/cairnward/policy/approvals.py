"""Approvals: asking the user, and remembering answers within a scope (spec §5.3).

Scopes: "once" (this action only), "conversation" or "environment" (same kind and target
from then on, until Cairnward restarts; persisted approvals arrive with the dashboard
inbox in M2). High-risk and user-only approvals are never remembered.
"""

from __future__ import annotations

import contextlib
import secrets
import sys
import threading
import time
from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Literal, Protocol, TextIO

Scope = Literal["once", "conversation", "environment"]
Risk = Literal["low", "medium", "high"]


@dataclass(frozen=True)
class ApprovalRequest:
    kind: str
    target: str
    risk: Risk
    reason: str
    environment: str | None
    conversation_id: str | None
    client: str | None
    initiated_by: str
    provenance: tuple[str, ...] = ()
    rememberable: bool = True
    run_id: str | None = None
    needs_secret: bool = False  # the approver also asks for a passphrase (e.g. encrypt)


@dataclass(frozen=True)
class ApprovalAnswer:
    granted: bool
    scope: Scope = "once"
    by: str = "user"
    # A passphrase typed in Cairnward's own prompt; never stored, logged or shown.
    secret: str | None = field(default=None, repr=False)


class Approver(Protocol):
    def request(self, req: ApprovalRequest) -> ApprovalAnswer: ...


class DenyApprover:
    """Used when nobody can be asked (no terminal, no dashboard): the safe answer is no."""

    def request(self, req: ApprovalRequest) -> ApprovalAnswer:
        return ApprovalAnswer(granted=False, by="no-approver")


@dataclass
class ScriptedApprover:
    """For tests and automation: answers from a function, records what was asked."""

    answer: Callable[[ApprovalRequest], ApprovalAnswer]
    asked: list[ApprovalRequest] = field(default_factory=list)

    def request(self, req: ApprovalRequest) -> ApprovalAnswer:
        self.asked.append(req)
        return self.answer(req)


class TerminalApprover:
    """Asks on the controlling terminal: y = once, c = this conversation,
    e = this environment, anything else = no."""

    def __init__(self, stdin: TextIO | None = None, stdout: TextIO | None = None):
        self.stdin = stdin or sys.stdin
        self.stdout = stdout or sys.stderr

    def request(self, req: ApprovalRequest) -> ApprovalAnswer:
        out = self.stdout
        out.write(f"\n[cairnward] Approval needed ({req.risk} risk)\n")
        out.write(f"  action : {req.kind}\n  target : {req.target}\n  why    : {req.reason}\n")
        scope_bits = [f"environment {req.environment}" if req.environment else "",
                      f"client {req.client}" if req.client else "",
                      f"started by {req.initiated_by}"]  # fmt: skip
        out.write(f"  where  : {', '.join(b for b in scope_bits if b)}\n")
        if req.provenance:
            out.write("  NOTE   : untrusted content entered the conversation since your last "
                      "message:\n")  # fmt: skip
            for item in req.provenance:
                out.write(f"           - {item}\n")
        options = "[y] once  [c] this conversation  [e] this environment  [n] no" if (
            req.rememberable) else "[y] yes, once  [n] no"  # fmt: skip
        out.write(f"  {options}: ")
        out.flush()
        reply = (self.stdin.readline() or "").strip().lower()
        if reply in {"y", "yes"}:
            return ApprovalAnswer(True, "once")
        if req.rememberable and reply == "c" and req.conversation_id:
            return ApprovalAnswer(True, "conversation")
        if req.rememberable and reply == "e":
            return ApprovalAnswer(True, "environment")
        return ApprovalAnswer(False)


@dataclass
class ApprovalMemory:
    """Remembered grants for (kind, target) within a conversation or environment."""

    _grants: set[tuple[str, str, str, str]] = field(default_factory=set)

    @staticmethod
    def _key(scope: Scope, req: ApprovalRequest) -> tuple[str, str, str, str] | None:
        if scope == "conversation" and req.conversation_id:
            return ("conversation", req.conversation_id, req.kind, req.target)
        if scope == "environment":
            return ("environment", req.environment or "", req.kind, req.target)
        return None

    def remember(self, answer: ApprovalAnswer, req: ApprovalRequest) -> None:
        if not (answer.granted and req.rememberable):
            return
        key = self._key(answer.scope, req)
        if key:
            self._grants.add(key)

    def granted(self, req: ApprovalRequest) -> Scope | None:
        if not req.rememberable:
            return None
        scopes: tuple[Scope, ...] = ("conversation", "environment")
        for scope in scopes:
            key = self._key(scope, req)
            if key and key in self._grants:
                return scope
        return None


LATE_S = 600  # how long a request the client gave up on can still be answered


def _retry_key(r: ApprovalRequest) -> tuple[str | None, ...]:
    return (r.kind, r.target, r.client, r.conversation_id, r.run_id, r.environment)


@dataclass
class _Pending:
    id: str
    req: ApprovalRequest
    created: float
    done: threading.Event = field(default_factory=threading.Event)
    answer: ApprovalAnswer | None = None
    gave_up: float | None = None  # when the waiting request timed out


class ApprovalInbox:
    """Pending approvals, answered from `cairnward approvals` (and later the dashboard).

    `request()` is called from a worker thread (the policy engine is synchronous); it waits
    until the request is answered or the timeout passes, which counts as "no".

    Retries (FR-POL-5): a request whose client gave up stays listed as "gave up" for
    LATE_S; answering yes then approves the client's next identical request once, so a
    retry doesn't ask again. Batching (FR-POL-3): `answer_similar` answers every pending
    request with the same kind and target at once.
    """

    def __init__(
        self, timeout_s: float = 120, notify: Callable[[dict[str, object]], None] | None = None
    ):
        self.timeout_s = timeout_s
        self.notify = notify
        self._pending: dict[str, _Pending] = {}
        self._retry: dict[tuple[str | None, ...], float] = {}
        self._lock = threading.Condition()
        self._version = 0
        self._closed = False

    def close(self) -> None:
        """Shutting down: refuse everything pending and wake every waiter."""
        with self._lock:
            self._closed = True
            for item in self._pending.values():
                if not item.done.is_set():
                    item.answer = ApprovalAnswer(False, by="shutdown")
                    item.done.set()
            self._changed()

    def _changed(self) -> None:
        self._version += 1
        self._lock.notify_all()

    def request(self, req: ApprovalRequest) -> ApprovalAnswer:
        now = time.time()
        if self._closed:
            return ApprovalAnswer(False, by="shutdown")
        with self._lock:
            self._expire(now)
            key = _retry_key(req)
            if not req.needs_secret and self._retry.pop(key, 0) > now:
                return ApprovalAnswer(True, "once", by="user (answered after a retry)")
            item = _Pending(secrets.token_hex(6), req, now)
            self._pending[item.id] = item
            self._changed()
        if self.notify is not None:
            with contextlib.suppress(Exception):  # a failed notice must not decide anything
                self.notify(self._describe(item))
        answered = item.done.wait(self.timeout_s)
        with self._lock:
            if answered:
                self._pending.pop(item.id, None)
            else:
                item.gave_up = time.time()  # still answerable for a retry
            self._changed()
        if not answered or item.answer is None:
            return ApprovalAnswer(False, by="timeout")
        return item.answer

    def _expire(self, now: float) -> None:
        for pid, p in list(self._pending.items()):
            if p.gave_up is not None and now - p.gave_up > LATE_S:
                del self._pending[pid]
        for key, until in list(self._retry.items()):
            if until < now:
                del self._retry[key]

    def answer(self, request_id: str, granted: bool, scope: Scope = "once", by: str = "user",
               secret: str | None = None) -> bool:  # fmt: skip
        with self._lock:
            item = self._pending.get(request_id)
            if item is None or item.done.is_set():
                return False
            if not item.req.rememberable:
                scope = "once"
            if item.gave_up is not None:  # the client stopped waiting: applies to its retry
                del self._pending[request_id]
                if granted:
                    self._retry[_retry_key(item.req)] = time.time() + LATE_S
                self._changed()
                return True
            item.answer = ApprovalAnswer(granted, scope, by, secret)
            item.done.set()
            return True

    def answer_similar(self, request_id: str, granted: bool, scope: Scope = "once",
                       by: str = "user") -> int:  # fmt: skip
        """Answer this request and every other pending one with the same kind and target."""
        with self._lock:
            first = self._pending.get(request_id)
            if first is None:
                return 0
            same = [p.id for p in self._pending.values()
                    if (p.req.kind, p.req.target) == (first.req.kind, first.req.target)
                    and not p.done.is_set() and not p.req.needs_secret]  # fmt: skip
        return sum(self.answer(pid, granted, scope, by) for pid in same)

    @property
    def version(self) -> int:
        with self._lock:
            return self._version

    def pending(self) -> list[dict[str, object]]:
        with self._lock:
            self._expire(time.time())
            items = [p for p in self._pending.values() if not p.done.is_set()]
            out = []
            for p in items:
                d = self._describe(p)
                d["similar"] = sum(
                    1 for q in items
                    if q is not p and (q.req.kind, q.req.target) == (p.req.kind, p.req.target)
                )  # fmt: skip
                out.append(d)
            return out

    def wait_for_change(self, version: int, timeout: float) -> int:
        """Block until the inbox changes after `version` (or timeout); returns the new version."""
        with self._lock:
            self._lock.wait_for(lambda: self._version != version or self._closed, timeout)
            return self._version

    @staticmethod
    def _describe(p: _Pending) -> dict[str, object]:
        r = p.req
        return {
            "id": p.id, "kind": r.kind, "target": r.target, "risk": r.risk, "reason": r.reason,
            "environment": r.environment, "client": r.client, "run_id": r.run_id,
            "conversation_id": r.conversation_id, "initiated_by": r.initiated_by,
            "provenance": list(r.provenance), "rememberable": r.rememberable,
            "needs_secret": r.needs_secret, "gave_up": p.gave_up is not None,
            "age_s": round(time.time() - p.created, 1),
        }  # fmt: skip


class RemoteApprover:
    """For the egress process in isolated mode: asks the core's inbox over its socket."""

    def __init__(self, socket_path: str, timeout_s: float):
        import httpx

        self._client = httpx.Client(
            transport=httpx.HTTPTransport(uds=socket_path),
            base_url="http://cairnward",
            timeout=timeout_s + 10,
        )

    def request(self, req: ApprovalRequest) -> ApprovalAnswer:
        import dataclasses

        import httpx

        try:
            resp = self._client.post("/v1/approvals/ask", json=dataclasses.asdict(req))
            data = resp.json()
        except (httpx.HTTPError, ValueError):
            return ApprovalAnswer(False, by="no-approver")
        return ApprovalAnswer(bool(data.get("granted")), data.get("scope", "once"),
                              data.get("by", "user"))  # fmt: skip
