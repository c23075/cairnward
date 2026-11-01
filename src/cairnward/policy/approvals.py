"""Approvals: asking the user, and remembering answers within a scope (spec §5.3).

Scopes: "once" (this action only), "conversation" or "environment" (same kind and target
from then on, until Cairnward restarts; persisted approvals arrive with the dashboard
inbox in M2). High-risk and user-only approvals are never remembered.
"""

from __future__ import annotations

import sys
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
