"""Redaction (spec §5.4, FR-RED-1): what the scanner finds, and what happens to hosted
model calls and MCP tool arguments under redaction = off / warn / enforce."""

import json
from collections.abc import Callable
from typing import Any

import pytest
from hypothesis import given
from hypothesis import strategies as st

from cairnward.config.models import RedactionConfig
from cairnward.policy import ApprovalAnswer, ScriptedApprover
from cairnward.redaction import Scanner, message_texts, scan_texts, value_texts
from tests.test_gateway import gw  # noqa: F401  (fixture)

FOUND = [
    ("write to jane.doe@example.org", "email"),
    ("call +60 12-345 6789", "phone"),
    ("call (555) 123-4567", "phone"),
    ("card 4111 1111 1111 1111", "payment_card"),
    ("GB82 WEST 1234 5698 7654 32", "iban"),
    ("ssn 123-45-6789", "us_ssn"),
    ("IC 900101-14-5678", "national_id"),
    ("AKIAABCDEFGHIJKLMNOP", "aws_access_key"),
    ("token ghp_" + "a" * 36, "github_token"),
    ("sk-ant-api03-" + "x" * 30, "anthropic_key"),
    ("sk-proj-" + "y" * 30, "openai_key"),
    ("-----BEGIN RSA PRIVATE KEY-----", "private_key"),
    ('api_key: "abcd1234efgh"', "password_assignment"),
    ("eyJhbGciOiJIUzI1NiJ9.eyJzdWIiOiIxMjM0NTY3ODkwIn0.c2lnbmF0dXJlLXZhbHVl", "jwt"),
    ("cw-" + "k" * 40, "cairnward_key"),
]
NOT_FOUND = [
    "version 1.2.3.4, released 2026-11-01 at 10:30",
    "IP 192.168.1.10 port 8484",
    "order 1234 5678 9012 3456",  # fails the Luhn check
    "GB00WEST12345698765432",  # fails the IBAN checksum
    "a sk-short key",
    "the password is in the vault",
    "def f(x): return x * 2  # 12 345",
]


@pytest.mark.parametrize(("text", "kind"), FOUND)
def test_finds(text: str, kind: str) -> None:
    assert [f.kind for f in Scanner().scan(text)] == [kind]


@pytest.mark.parametrize("text", NOT_FOUND)
def test_no_false_alarm(text: str) -> None:
    assert Scanner().scan(text) == []


def test_custom_and_ignore() -> None:
    cfg = RedactionConfig.model_validate({
        "custom": [{"name": "project_code", "regex": r"\bPRJ-\d{4}\b", "category": "secret"}],
        "ignore": [r"@example\.org$"],
    })  # fmt: skip
    s = Scanner.from_config(cfg)
    found = s.scan("PRJ-1234 from me@example.org and you@corp.com")
    assert [(f.kind, f.category) for f in found] == [
        ("project_code", "secret"),
        ("email", "personal"),
    ]
    with pytest.raises(ValueError, match="regular expression"):
        RedactionConfig.model_validate({"ignore": ["("]})


def test_text_extraction() -> None:
    msgs: list[dict[str, Any]] = [
        {"role": "user", "content": [{"type": "text", "text": "a"}, {"type": "image_url"}]},
        {"role": "assistant", "content": None,
         "tool_calls": [{"function": {"name": "f", "arguments": "{\"x\": \"b\"}"}}]},
    ]  # fmt: skip
    assert list(message_texts(msgs)) == ["a", '{"x": "b"}']
    assert list(value_texts({"k": ["v", {"n": 1, "s": "w"}]})) == ["k", "v", "n", "s", "w"]
    report = scan_texts(Scanner(), ["x@y.org and x@y.org", "AKIAABCDEFGHIJKLMNOP"])
    assert report.kinds() == {"aws_access_key": 1, "email": 2}


@given(st.text(max_size=300))
def test_scanner_never_crashes_and_spans_are_valid(text: str) -> None:
    for f in Scanner().scan(text):
        assert 0 <= f.start < f.end <= len(text)


# ------------------------------------------------------------------ gateway


SECRET_MSG = [{"role": "user", "content": "deploy with AKIAABCDEFGHIJKLMNOP please"}]
EMAIL_MSG = [{"role": "user", "content": "email jane.doe@example.org the summary"}]


def cloudy(mode: str) -> dict[str, str]:
    return {"cloudy": f'backend = "cloud"\n[privacy]\nhosted_backends = "allow"\n'
                      f'network = "open"\nredaction = "{mode}"\n'}  # fmt: skip


async def test_enforce_blocks_secrets_to_hosted_backends(gw: Callable[..., Any]) -> None:  # noqa: F811
    g = await gw(cloudy("enforce"))
    resp = await g.chat("cw/cloudy:low", SECRET_MSG)
    assert resp.status_code == 403 and resp.json()["error"]["type"] == "redaction_blocked"
    assert "aws_access_key" in resp.json()["error"]["message"]
    assert g.cloud.requests == []
    rec = next(r for r in g.audit.records() if r.action == "redaction")
    assert rec.decision == "deny" and rec.details["kinds"] == {"aws_access_key": 1}
    assert "AKIA" not in json.dumps([r.details for r in g.audit.records()])
    # local models: content never leaves the machine, so nothing is scanned
    assert (await g.chat("cw/coding:low", SECRET_MSG)).status_code == 200


async def test_enforce_asks_for_personal_data(gw: Callable[..., Any]) -> None:  # noqa: F811
    g = await gw(cloudy("enforce"))
    approver = ScriptedApprover(lambda r: ApprovalAnswer(False))
    engine = g.client._transport.app.state.deps.engine
    engine.approver = approver
    resp = await g.chat("cw/cloudy:low", EMAIL_MSG)
    assert resp.status_code == 403 and "email" in resp.json()["error"]["message"]
    assert approver.asked[0].kind == "data.send" and approver.asked[0].target == "backend:cloud"
    approver.answer = lambda r: ApprovalAnswer(True, "conversation")
    assert (await g.chat("cw/cloudy:low", EMAIL_MSG)).status_code == 200
    assert len(g.cloud.requests) == 1
    # remembered for this conversation
    assert (await g.chat("cw/cloudy:low", EMAIL_MSG)).status_code == 200
    assert len(approver.asked) == 2


async def test_warn_flags_and_sends(gw: Callable[..., Any]) -> None:  # noqa: F811
    g = await gw(cloudy("warn"))
    resp = await g.chat("cw/cloudy:low", SECRET_MSG)
    assert resp.status_code == 200 and "aws_access_key x1" in resp.headers["X-Cairnward-Warning"]
    assert any(r.action == "redaction" and r.decision == "allow" for r in g.audit.records())


async def test_off_does_nothing(gw: Callable[..., Any]) -> None:  # noqa: F811
    g = await gw(cloudy("off"))
    resp = await g.chat("cw/cloudy:low", SECRET_MSG)
    assert resp.status_code == 200 and "X-Cairnward-Warning" not in resp.headers
    assert not any(r.action == "redaction" for r in g.audit.records())
