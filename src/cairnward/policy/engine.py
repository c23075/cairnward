"""Policy engine: allow / ask / deny for every action, from the privacy settings (spec §5.3).

Every rule returns a *base* decision with a risk tier. Then:
- base "deny" -> deny; base "allow" -> allow.
- base "ask" because a setting explicitly says ask (network = "ask", hosted_backends =
  "ask") -> always ask: the user chose to be asked.
- base "ask" from a risk tier -> the `approvals` setting decides: off = allow;
  high_only = ask only for high risk; tiered = ask for medium and high.
- user-only actions started by the model always ask (prompt injection must not be able to
  switch protections off unseen) and are never remembered.
An "ask" goes to the approver; with nobody to ask, the answer is deny.
"""

from __future__ import annotations

import fnmatch
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Literal

from cairnward.audit.log import AuditLog, Effect
from cairnward.config.loader import Config
from cairnward.config.privacy import EffectivePrivacy
from cairnward.policy.approvals import (
    ApprovalMemory,
    ApprovalRequest,
    Approver,
    DenyApprover,
    Risk,
)

Kind = Literal[
    "model.call",  # target: backend name
    "net.connect",  # target: host or host:port
    "fs.read",  # target: path
    "fs.write",  # target: path
    "exec",  # target: command line (summary)
    "tool.call",  # target: <mcp server>/<tool>
    "data.send",  # target: destination; personal data found by redaction (spec §5.4)
    # user-only actions (spec §5.8 / D46)
    "encrypt",
    "decrypt",
    "sync.change",
    "backend.enable_hosted",
    "mcp.approve",
    "isolation.disable",
]
USER_ONLY: frozenset[str] = frozenset(
    {"encrypt", "decrypt", "sync.change", "backend.enable_hosted", "mcp.approve",
     "isolation.disable"}
)  # fmt: skip
Initiator = Literal["user", "model", "cairnward"]
Final = Literal["allow", "deny"]
_RISK_ORDER = {"low": 0, "medium": 1, "high": 2}


class PolicyDenied(Exception):
    def __init__(self, decision: Decision):
        self.decision = decision
        super().__init__(
            f"{decision.action.kind} {decision.action.target}: denied ({decision.reason})"
        )


@dataclass(frozen=True)
class Action:
    kind: Kind
    target: str
    environment: str | None = None
    client: str | None = None
    conversation_id: str | None = None
    run_id: str | None = None
    initiated_by: Initiator = "model"
    actor: str = "cairnward"
    # Untrusted content (web pages, files, tool output) that entered the context since the
    # user's last message, e.g. ("read paper.pdf p.4",). Shown in user-only dialogs.
    provenance: tuple[str, ...] = ()
    details: dict[str, Any] = field(default_factory=dict)


@dataclass(frozen=True)
class Rule:
    base: Literal["allow", "ask", "deny"]
    risk: Risk
    reason: str
    explicit_ask: bool = False  # a setting itself says "ask"


@dataclass(frozen=True)
class Decision:
    action: Action
    decision: Final
    risk: Risk
    reason: str
    asked: bool = False
    approved_by: str | None = None
    scope: str | None = None
    advisory: bool = False  # isolation is off, so the OS doesn't enforce this
    # A passphrase the user typed in Cairnward's own prompt (e.g. encrypt); never audited.
    secret: str | None = field(default=None, repr=False)

    @property
    def allowed(self) -> bool:
        return self.decision == "allow"


def host_matches(host: str, pattern: str) -> bool:
    """'example.org', '*.example.org' (subdomains only) and optional ':port' on either side."""
    h_name, _, h_port = host.lower().rpartition(":") if ":" in host else (host.lower(), "", "")
    p_name, _, p_port = (
        pattern.lower().rpartition(":") if ":" in pattern else (pattern.lower(), "", "")
    )
    if p_port and p_port != h_port:
        return False
    if p_name.startswith("*."):
        return h_name.endswith(p_name[1:]) and h_name != p_name[2:]
    return fnmatch.fnmatchcase(h_name, p_name) if "*" in p_name else h_name == p_name


def _inside(path: Path, roots: list[Path]) -> bool:
    resolved = path.expanduser().resolve(strict=False)
    return any(resolved.is_relative_to(r.expanduser().resolve(strict=False)) for r in roots)


class PolicyEngine:
    def __init__(
        self,
        config: Config,
        *,
        approver: Approver | None = None,
        audit: AuditLog | None = None,
        memory: ApprovalMemory | None = None,
        extra_roots: list[Path] | None = None,
    ):
        self.config = config
        self.approver: Approver = approver or DenyApprover()
        self.audit = audit
        self.memory = memory or ApprovalMemory()
        # Folders always writable by Cairnward itself (artifacts, its data folder).
        self.extra_roots = extra_roots or [config.paths.data_dir]

    # ------------------------------------------------------------------ rules

    def privacy_for(self, action: Action) -> EffectivePrivacy:
        env = action.environment if action.environment in self.config.environments else None
        return self.config.privacy(env)

    def _workspace(self, action: Action) -> list[Path]:
        env = self.config.environments.get(action.environment or "")
        return list(env.workspace) if env else []

    def _network_rule(self, eff: EffectivePrivacy, host: str, what: str) -> Rule:
        mode = eff.value("network")
        listed = any(host_matches(host, p) for p in eff.network_allow_hosts)
        if mode == "open":
            return Rule("allow", "low", f"network = open: {what} allowed")
        if mode == "off":
            return Rule("deny", "high", f"network = off: no {what}")
        if listed:
            return Rule("allow", "low", f"{host} is in network_allow_hosts")
        if mode == "allowlist":
            return Rule("deny", "high", f"network = allowlist and {host} is not listed")
        return Rule(
            "ask", "medium", f"network = ask: {host} is not approved yet", explicit_ask=True
        )

    def rule_for(self, action: Action) -> Rule:
        eff = self.privacy_for(action)
        kind, target = action.kind, action.target

        if kind in USER_ONLY:
            if action.initiated_by == "model":
                return Rule("ask", "high",
                            "user-only action requested by the model: confirm it was your intent",
                            explicit_ask=True)  # fmt: skip
            return Rule("ask", "high", f"user-only action: {kind}")

        if kind == "model.call":
            backend = self.config.backends.backends.get(target)
            if backend is None:
                return Rule("deny", "high", f"unknown backend '{target}'")
            if backend.location == "local":
                return Rule("allow", "low", "local model server")
            host = backend.url.split("//", 1)[-1].split("/", 1)[0]
            if backend.trains_on_prompts and eff.value("training_backends") == "scratch_only":
                env = self.config.environments.get(action.environment or "")
                if env is None or not env.scratch:
                    return Rule("deny", "high", f"'{target}' may train on prompts: allowed only "
                                "in environments marked scratch (training_backends = "
                                "scratch_only)")  # fmt: skip
            if backend.location == "hosted":
                hosted = eff.value("hosted_backends")
                if hosted == "deny":
                    return Rule("deny", "high", "hosted_backends = deny")
                net = self._network_rule(eff, host, "hosted model calls")
                if net.base == "deny":
                    return net
                if hosted == "ask":
                    return Rule("ask", "high", "hosted_backends = ask", explicit_ask=True)
                return Rule("allow", "medium", "hosted_backends = allow")
            return self._network_rule(eff, host, "LAN model calls")

        if kind == "net.connect":
            return self._network_rule(eff, target, "outbound connections")

        if kind in ("fs.read", "fs.write"):
            path = Path(target)
            roots = self._workspace(action)
            if _inside(path, roots) or _inside(path, self.extra_roots):
                return Rule("allow", "low", "inside the workspace")
            if kind == "fs.read":
                return Rule("ask", "medium", "read outside the workspace")
            return Rule("ask", "high", "write outside the workspace")

        if kind == "data.send":
            kinds = ", ".join(sorted(action.details.get("kinds", {}))) or "personal data"
            return Rule("ask", "medium", f"{kinds} would be sent to {target}", explicit_ask=True)

        if kind == "tool.call":
            server_name, _, tool = target.partition("/")
            server = self.config.mcp.servers.get(server_name)
            env = self.config.environments.get(action.environment or "")
            if server_name == "cairnward" and env is not None:  # Cairnward's own tools
                research_tools = ("lit_search", "lit_ask", "lit_review_start")
                needed = "research" if tool in research_tools else None
                if tool.startswith("artifact_"):
                    needed = "artifacts"
                if needed and needed in env.tools:
                    return Rule("allow", "low", f"Cairnward's {tool} (read-only, local)")
                if (
                    tool == "lit_find"
                    and env.research_sources
                    and ({"research", "scholar"} & set(env.tools))
                ):  # each host it contacts is checked again as net.connect
                    return Rule("allow", "low", "Cairnward's lit_find (online sources on here)")
                return Rule("deny", "high", f"Cairnward's {tool} isn't enabled here")
            if server is None or env is None or server_name not in env.mcp_servers:
                return Rule("deny", "high", f"MCP server '{server_name}' isn't enabled here")
            setting = server.tools.get(tool, server.default_tool)
            if setting == "deny":
                return Rule("deny", "high", f"{tool} is denied in mcp.toml")
            if setting == "allow":
                return Rule("allow", "low", f"{tool} is allowed in mcp.toml")
            if setting == "ask":
                return Rule("ask", "medium", f"{tool}: mcp.toml says ask", explicit_ask=True)
            return Rule("ask", "medium", f"MCP tool {server_name}/{tool}")

        if kind == "exec":
            if eff.value("isolation") == "on":
                return Rule("allow", "low", "runs in the sandbox")
            return Rule("ask", "high", "isolation is off: the command runs unsandboxed")

        return Rule("deny", "high", f"no rule for action kind '{kind}'")

    # ------------------------------------------------------------------ decide

    def _needs_prompt(self, rule: Rule, eff: EffectivePrivacy) -> bool:
        if rule.explicit_ask:
            return True
        approvals = eff.value("approvals")
        if approvals == "off":
            return False
        threshold = "high" if approvals == "high_only" else "medium"
        return _RISK_ORDER[rule.risk] >= _RISK_ORDER[threshold]

    def check(self, action: Action, *, dry_run: bool = False) -> Decision:
        """Decide; asks the approver when needed (unless dry_run)."""
        eff = self.privacy_for(action)
        rule = self.rule_for(action)
        advisory = eff.value("isolation") == "off" and action.kind in ("net.connect", "fs.write")

        def done(final: Final, **kw: Any) -> Decision:
            return Decision(action, final, rule.risk, kw.pop("reason", rule.reason),
                            advisory=advisory, **kw)  # fmt: skip

        if rule.base == "deny":
            return done("deny")
        if rule.base == "allow":
            return done("allow")
        if not self._needs_prompt(rule, eff):
            return done("allow", reason=f"{rule.reason} (approvals = {eff.value('approvals')})")

        req = ApprovalRequest(
            kind=action.kind, target=action.target, risk=rule.risk, reason=rule.reason,
            environment=action.environment, conversation_id=action.conversation_id,
            client=action.client, initiated_by=action.initiated_by,
            provenance=action.provenance,
            run_id=action.run_id,
            rememberable=rule.risk != "high" and action.kind not in USER_ONLY,
            needs_secret=action.kind == "encrypt",
        )  # fmt: skip
        remembered = self.memory.granted(req)
        if remembered:
            return done("allow", reason=f"{rule.reason}; approved earlier for this {remembered}",
                        approved_by="user", scope=remembered)  # fmt: skip
        if dry_run:
            return done("deny", reason=f"{rule.reason} (would ask)", asked=False)
        answer = self.approver.request(req)
        self.memory.remember(answer, req)
        if answer.granted:
            return done("allow", asked=True, approved_by=answer.by, scope=answer.scope,
                        secret=answer.secret)  # fmt: skip
        return done(
            "deny", reason=f"{rule.reason}: not approved", asked=True, approved_by=answer.by
        )

    # ------------------------------------------------------------------ audit helpers

    def _details(self, decision: Decision) -> dict[str, Any]:
        action = decision.action
        return {
            "risk": decision.risk,
            "reason": decision.reason,
            "asked": decision.asked,
            "approved_by": decision.approved_by,
            "scope": decision.scope,
            "advisory": decision.advisory,
            "initiated_by": action.initiated_by,
            "provenance": list(action.provenance),
            **action.details,
        }

    @staticmethod
    def _common(action: Action) -> dict[str, Any]:
        return dict(
            actor=action.actor, target=action.target, environment=action.environment,
            client=action.client, run_id=action.run_id, conversation_id=action.conversation_id,
        )  # fmt: skip

    def audit_deny(self, decision: Decision) -> None:
        """One `deny` event for a refused action."""
        if self.audit:
            action = decision.action
            self.audit.append(
                action.kind, phase="event", decision="deny",
                details=self._details(decision), **self._common(action),
            )  # fmt: skip

    def audit_pre(self, decision: Decision, params: bytes | str | None = None) -> int | None:
        """The `pre` record of an allowed action; returns its seq for `audit_post`."""
        if not self.audit:
            return None
        from cairnward.audit.log import digest

        rec = self.audit.append(
            decision.action.kind, phase="pre", decision="allow",
            params_digest=digest(params) if params is not None else None,
            details=self._details(decision), **self._common(decision.action),
        )  # fmt: skip
        return rec.seq if rec else None

    def audit_post(
        self,
        action: Action,
        pre_seq: int | None,
        *,
        outcome: str,
        result_digest: str | None = None,
        details: dict[str, Any] | None = None,
    ) -> None:
        if self.audit:
            self.audit.append(action.kind, phase="post", outcome=outcome, pre_seq=pre_seq,
                              result_digest=result_digest, details=details or {},
                              **self._common(action))  # fmt: skip

    # ------------------------------------------------------------------ guard

    @contextmanager
    def guard(self, action: Action, *, params: bytes | str | None = None) -> Iterator[Effect]:
        """Check, record the decision, and run the block only if allowed.

        Denied: one audit event (`decision = deny`), then PolicyDenied.
        Allowed: pre record (with decision and how it was approved), the block, post record.
        """
        decision = self.check(action)
        if not decision.allowed:
            self.audit_deny(decision)
            raise PolicyDenied(decision)
        pre_seq = self.audit_pre(decision, params)
        eff = Effect()
        try:
            yield eff
        except BaseException as exc:
            self.audit_post(action, pre_seq, outcome="error",
                            details={**eff.details, "error": type(exc).__name__})  # fmt: skip
            raise
        self.audit_post(action, pre_seq, outcome="ok", result_digest=eff.result_digest,
                        details=eff.details)  # fmt: skip
