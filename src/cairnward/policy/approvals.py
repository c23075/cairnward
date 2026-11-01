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


@dataclass(frozen=True)
class ApprovalAnswer:
    granted: bool
    scope: Scope = "once"
    by: str = "user"


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


@dataclass
class _Pending:
    id: str
    req: ApprovalRequest
    created: float
    done: threading.Event = field(default_factory=threading.Event)
    answer: ApprovalAnswer | None = None


class ApprovalInbox:
    """Pending approvals, answered from `cairnward approvals` (and later the dashboard).

    `request()` is called from a worker thread (the policy engine is synchronous); it waits
    until the request is answered or the timeout passes, which counts as "no".
    """

    def __init__(
        self, timeout_s: float = 120, notify: Callable[[dict[str, object]], None] | None = None
    ):
        self.timeout_s = timeout_s
        self.notify = notify
        self._pending: dict[str, _Pending] = {}
        self._lock = threading.Condition()
        self._version = 0

    def request(self, req: ApprovalRequest) -> ApprovalAnswer:
        item = _Pending(secrets.token_hex(6), req, time.time())
        with self._lock:
            self._pending[item.id] = item
            self._version += 1
            self._lock.notify_all()
        if self.notify is not None:
            with contextlib.suppress(Exception):  # a failed notice must not decide anything
                self.notify(self._describe(item))
        answered = item.done.wait(self.timeout_s)
        with self._lock:
            self._pending.pop(item.id, None)
            self._version += 1
            self._lock.notify_all()
        if not answered or item.answer is None:
            return ApprovalAnswer(False, by="timeout")
        return item.answer

    def answer(
        self, request_id: str, granted: bool, scope: Scope = "once", by: str = "user"
    ) -> bool:
        with self._lock:
            item = self._pending.get(request_id)
            if item is None or item.done.is_set():
                return False
            if not item.req.rememberable:
                scope = "once"
            item.answer = ApprovalAnswer(granted, scope, by)
            item.done.set()
            return True

    @property
    def version(self) -> int:
        with self._lock:
            return self._version

    def pending(self) -> list[dict[str, object]]:
        with self._lock:
            return [self._describe(p) for p in self._pending.values() if not p.done.is_set()]

    def wait_for_change(self, version: int, timeout: float) -> int:
        """Block until the inbox changes after `version` (or timeout); returns the new version."""
        with self._lock:
            self._lock.wait_for(lambda: self._version != version, timeout)
            return self._version

    @staticmethod
    def _describe(p: _Pending) -> dict[str, object]:
        r = p.req
        return {
            "id": p.id, "kind": r.kind, "target": r.target, "risk": r.risk, "reason": r.reason,
            "environment": r.environment, "client": r.client, "run_id": r.run_id,
            "conversation_id": r.conversation_id, "initiated_by": r.initiated_by,
            "provenance": list(r.provenance), "rememberable": r.rememberable,
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
