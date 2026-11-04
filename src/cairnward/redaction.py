"""Redaction scanner for content leaving through Cairnward (spec §5.4, FR-RED-1).

Applies to traffic Cairnward itself sends on: model calls to non-local backends and
arguments of MCP tool calls. A light, local, regex-based scanner:
- secrets: private keys, cloud and service tokens, JWTs, Cairnward keys, password
  assignments;
- personal data: emails, phone numbers, payment card numbers (Luhn-checked), IBANs
  (checksum-verified), ID-like numbers;
- custom patterns and ignore rules from `[redaction]` in cairnward.toml.

`redaction = "warn"`: findings are recorded (kinds and counts only, never the text) and
flagged to the client. `"enforce"`: secrets block the request; personal data asks the
user (remembered per conversation or environment when approved that way).
Content is never altered (FR-GW-7): it's sent as it is, or not at all.
"""

from __future__ import annotations

import bisect
import re
from collections import Counter
from collections.abc import Iterable, Iterator
from dataclasses import dataclass, field
from typing import Any, Literal

Category = Literal["secret", "personal"]

SECRETS: list[tuple[str, re.Pattern[str]]] = [
    (name, re.compile(rx))
    for name, rx in [
        ("private_key", r"-----BEGIN (?:[A-Z0-9]+ )*PRIVATE KEY-----"),
        ("aws_access_key", r"\b(?:AKIA|ASIA)[0-9A-Z]{16}\b"),
        ("github_token", r"\b(?:gh[pousr]_[A-Za-z0-9]{36,}|github_pat_[A-Za-z0-9_]{22,})"),
        ("anthropic_key", r"\bsk-ant-[A-Za-z0-9_-]{20,}"),
        ("openai_key", r"\bsk-(?:proj-|svcacct-)?[A-Za-z0-9_-]{20,}"),
        ("slack_token", r"\bxox[abposr]-[A-Za-z0-9-]{10,}"),
        ("google_api_key", r"\bAIza[0-9A-Za-z_-]{35}"),
        ("stripe_key", r"\b(?:sk|rk)_live_[0-9A-Za-z]{20,}"),
        ("huggingface_token", r"\bhf_[A-Za-z0-9]{30,}"),
        ("jwt", r"\beyJ[A-Za-z0-9_-]{8,}\.eyJ[A-Za-z0-9_-]{8,}\.[A-Za-z0-9_-]{8,}"),
        ("cairnward_key", r"\bcw-[A-Za-z0-9_-]{30,}"),
        ("password_assignment",
         r"(?i)\b(?:password|passwd|pwd|secret|api[_-]?key|access[_-]?token|auth[_-]?token)"
         r"\b[\"']?\s*[:=]\s*[\"']?[^\s\"']{8,}"),
    ]
]  # fmt: skip

_EMAIL = re.compile(r"\b[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}\b")
_PHONE = re.compile(r"(?<![\w+])\+?\(?\d{1,4}\)?(?:[\s.-]\(?\d{1,4}\)?){2,5}(?![\w-])")
_CARD = re.compile(r"\b\d(?:[ -]?\d){12,18}\b")
_IBAN = re.compile(r"\b[A-Z]{2}\d{2}(?: ?[A-Z0-9]{4}){2,7}(?: ?[A-Z0-9]{1,3})?\b")
_SSN = re.compile(r"\b(?!000|666|9\d\d)\d{3}-(?!00)\d{2}-(?!0000)\d{4}\b")
_NATIONAL_ID = re.compile(r"\b\d{6}-\d{2}-\d{4}\b")  # e.g. Malaysian NRIC layout


def _luhn(digits: str) -> bool:
    total = 0
    for i, ch in enumerate(reversed(digits)):
        n = int(ch)
        if i % 2:
            n = n * 2 - 9 if n > 4 else n * 2
        total += n
    return total % 10 == 0


def _iban_ok(value: str) -> bool:
    s = value.replace(" ", "")
    if not 15 <= len(s) <= 34:
        return False
    moved = s[4:] + s[:4]
    return int("".join(str(int(c, 36)) for c in moved)) % 97 == 1


@dataclass(frozen=True)
class Finding:
    kind: str
    category: Category
    start: int
    end: int


@dataclass
class Scanner:
    custom: list[tuple[str, Category, re.Pattern[str]]] = field(default_factory=list)
    ignore: list[re.Pattern[str]] = field(default_factory=list)
    presidio: Any = None  # redaction_engines.presidio.Presidio (optional, FR-RED-2)
    presidio_entities: list[str] = field(default_factory=list)
    presidio_min_score: float = 0.6
    unavailable: list[str] = field(default_factory=list)  # optional detectors that failed

    @classmethod
    def from_config(cls, redaction: Any, paths: Any = None) -> Scanner:
        presidio = None
        if getattr(redaction, "presidio", False) and paths is not None:
            from cairnward.redaction_engines import presidio as presidio_engine

            presidio = presidio_engine.shared(paths)
        return cls(
            [(c.name, c.category, re.compile(c.regex)) for c in redaction.custom],
            [re.compile(rx) for rx in redaction.ignore],
            presidio,
            list(getattr(redaction, "presidio_entities", [])),
            float(getattr(redaction, "presidio_min_score", 0.6)),
        )

    def _presidio(self, text: str) -> Iterator[Finding]:
        if self.presidio is None or not text.strip():
            return
        from cairnward.redaction_engines.presidio import PresidioError

        try:
            found = self.presidio.analyze(text, self.presidio_entities, self.presidio_min_score)
        except PresidioError as exc:
            if not self.unavailable:
                self.unavailable.append(f"presidio: {exc}")
            return
        for f in found:
            yield Finding(f"presidio:{str(f['type']).lower()}", "personal", int(f["start"]),
                          int(f["end"]))  # fmt: skip

    def _personal(self, text: str) -> Iterator[Finding]:
        for m in _EMAIL.finditer(text):
            yield Finding("email", "personal", m.start(), m.end())
        for m in _CARD.finditer(text):
            digits = re.sub(r"\D", "", m.group())
            if 13 <= len(digits) <= 19 and _luhn(digits):
                yield Finding("payment_card", "personal", m.start(), m.end())
        for m in _IBAN.finditer(text):
            if _iban_ok(m.group()):
                yield Finding("iban", "personal", m.start(), m.end())
        for m in _SSN.finditer(text):
            yield Finding("us_ssn", "personal", m.start(), m.end())
        for m in _NATIONAL_ID.finditer(text):
            yield Finding("national_id", "personal", m.start(), m.end())
        for m in _PHONE.finditer(text):
            digits = re.sub(r"\D", "", m.group())
            if 9 <= len(digits) <= 15 and not re.fullmatch(r"[\d.]+", m.group()):
                yield Finding("phone", "personal", m.start(), m.end())

    def scan(self, text: str) -> list[Finding]:
        """Non-overlapping findings; secrets win over personal data on the same text."""
        found: list[Finding] = []
        starts: list[int] = []  # accepted findings, sorted by start (they never overlap)
        by_start: list[Finding] = []

        def overlaps(f: Finding) -> bool:
            i = bisect.bisect_left(starts, f.start)
            # only the neighbours can overlap, since accepted findings are disjoint
            return (i > 0 and by_start[i - 1].end > f.start) or (
                i < len(by_start) and by_start[i].start < f.end
            )

        def ignored(f: Finding) -> bool:
            value = text[f.start : f.end]
            return any(rx.search(value) for rx in self.ignore)

        candidates: list[Finding] = []
        for name, rx in SECRETS:
            candidates += [Finding(name, "secret", m.start(), m.end()) for m in rx.finditer(text)]
        for name, category, rx in self.custom:
            candidates += [Finding(name, category, m.start(), m.end()) for m in rx.finditer(text)]
        candidates += list(self._personal(text))
        candidates += list(self._presidio(text))  # after the patterns: they win on overlaps
        for f in candidates:  # in priority order: built-in secrets, custom, personal
            if f.end > f.start and not overlaps(f) and not ignored(f):
                found.append(f)
                i = bisect.bisect_left(starts, f.start)
                starts.insert(i, f.start)
                by_start.insert(i, f)
        return sorted(found, key=lambda f: f.start)


@dataclass(frozen=True)
class Report:
    secrets: Counter[str]
    personal: Counter[str]
    unavailable: list[str] = field(default_factory=list)

    @property
    def empty(self) -> bool:
        return not self.secrets and not self.personal

    def kinds(self) -> dict[str, int]:
        return dict(sorted((self.secrets + self.personal).items()))

    def summary(self) -> str:
        return ", ".join(f"{k} x{n}" for k, n in self.kinds().items())


def scan_texts(scanner: Scanner, texts: Iterable[str]) -> Report:
    secrets: Counter[str] = Counter()
    personal: Counter[str] = Counter()
    for text in texts:
        for f in scanner.scan(text):
            (secrets if f.category == "secret" else personal)[f.kind] += 1
    return Report(secrets, personal, list(scanner.unavailable))


def message_texts(messages: list[dict[str, Any]]) -> Iterator[str]:
    """The text an OpenAI-style conversation would send (content parts, tool calls)."""
    for m in messages:
        content = m.get("content")
        if isinstance(content, str):
            yield content
        elif isinstance(content, list):
            for part in content:
                if isinstance(part, dict) and isinstance(part.get("text"), str):
                    yield part["text"]
        for call in m.get("tool_calls") or []:
            fn = call.get("function") if isinstance(call, dict) else None
            if isinstance(fn, dict) and isinstance(fn.get("arguments"), str):
                yield fn["arguments"]


def value_texts(value: Any) -> Iterator[str]:
    """Every string inside a JSON value (e.g. tool arguments)."""
    if isinstance(value, str):
        yield value
    elif isinstance(value, dict):
        for k, v in value.items():
            yield str(k)
            yield from value_texts(v)
    elif isinstance(value, list):
        for v in value:
            yield from value_texts(v)


# ------------------------------------------------------------------ decisions


@dataclass(frozen=True)
class Verdict:
    allowed: bool
    reason: str = ""
    warning: str = ""
    report: Report | None = None


def check_outbound(
    engine: Any,
    texts: Iterable[str],
    *,
    destination: str,
    environment: str,
    client: str | None,
    run_id: str | None = None,
    conversation_id: str | None = None,
    initiated_by: str = "model",
) -> Verdict:
    """Scan content about to leave through Cairnward and decide (synchronous: may ask)."""
    from cairnward.policy.engine import Action

    cfg = engine.config
    mode = cfg.privacy(environment).value("redaction")
    if mode == "off":
        return Verdict(True)
    report = scan_texts(Scanner.from_config(cfg.main.redaction, cfg.paths), texts)
    if report.unavailable and engine.audit:  # say so: fewer detectors than configured
        engine.audit.append("redaction", actor="cairnward", target=destination,
                            environment=environment, client=client, run_id=run_id,
                            details={"mode": mode, "unavailable": report.unavailable})  # fmt: skip
    if report.empty:
        return Verdict(True)
    common: dict[str, Any] = dict(
        target=destination, environment=environment, client=client, run_id=run_id,
        conversation_id=conversation_id,
    )  # fmt: skip
    details = {"mode": mode, "kinds": report.kinds()}

    def record(decision: str, **extra: Any) -> None:
        if engine.audit:
            engine.audit.append("redaction", actor="cairnward", decision=decision,
                                details={**details, **extra}, **common)  # fmt: skip

    if mode == "warn":
        record("allow")
        return Verdict(True, warning=f"possible sensitive data sent to {destination}: "
                       f"{report.summary()} (redaction = warn)", report=report)  # fmt: skip
    if report.secrets:
        record("deny", reason="secrets are never sent")
        found = ", ".join(sorted(report.secrets))
        return Verdict(False, reason=f"the request contains what looks like a secret ({found}); "
                       "remove it, or add an ignore rule under [redaction] if it isn't one",
                       report=report)  # fmt: skip
    action = Action(
        kind="data.send",
        actor="cairnward",
        initiated_by=initiated_by,  # type: ignore[arg-type]
        details={"kinds": dict(report.personal)},
        **common,
    )
    decision = engine.check(action)
    if not decision.allowed:
        engine.audit_deny(decision)
        return Verdict(False, reason=decision.reason, report=report)
    record("allow", approved_by=decision.approved_by, scope=decision.scope)
    return Verdict(True, report=report)
