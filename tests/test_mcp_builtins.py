"""Skills served as MCP prompts/resources (FR-MCP-4) and Cairnward's own artifact tools."""

import shutil
import sys
from pathlib import Path
from typing import Any

import httpx
import pytest
from fastapi import FastAPI

from cairnward import skills
from cairnward.audit import AuditLog
from cairnward.backends import Backends
from cairnward.config import load_config
from cairnward.config.paths import Paths
from cairnward.mcp.http import add_mcp_routes
from cairnward.mcp.hub import McpHub
from cairnward.mcp.launch import LocalLauncher
from cairnward.mcp.pins import PinStore
from cairnward.policy import PolicyEngine
from cairnward.policy.provenance import ProvenanceTracker
from tests.fakes.backends import FakeOllama

SKILL = """---
name: apa-review
description: Check references against APA 7
---
Check every reference in the draft against APA 7th edition.
"""


@pytest.fixture
def paths(tmp_path: Path) -> Paths:
    p = Paths(tmp_path / "cfg", tmp_path / "data", tmp_path / "state")
    (p.config_dir / "environments").mkdir(parents=True)
    sk = p.config_dir / "skills" / "apa-review"
    sk.mkdir(parents=True)
    (sk / "SKILL.md").write_text(SKILL)
    (sk / "examples.md").write_text("Smith, J. (2020). Title. Journal, 1(2), 3-4.")
    (sk / ".secret").write_text("hidden")
    (p.config_dir / "skills" / "other").mkdir()
    (p.config_dir / "skills" / "other" / "SKILL.md").write_text("not enabled")
    if sys.platform != "win32":
        (sk / "escape.txt").symlink_to("/etc/hostname")
    (p.config_dir / "environments" / "lit.toml").write_text(
        'skills = ["apa-review"]\ntools = ["research"]\n[privacy]\nisolation = "off"\n'
    )
    return p


def test_skill_loading(paths: Paths) -> None:
    found = skills.load(paths.config_dir / "skills")
    s = found["apa-review"]
    assert s.description == "Check references against APA 7" and s.body.startswith("Check every")
    assert skills.files(s) == ["examples.md"]  # hidden files and links out are not served
    with pytest.raises(skills.SkillError):
        skills.read(s, "../other/SKILL.md")


async def setup(paths: Paths) -> tuple[httpx.AsyncClient, McpHub, AuditLog, ProvenanceTracker]:
    cfg = load_config(paths)
    fake = FakeOllama()
    backends = Backends(cfg.backends, transport_for=lambda n: httpx.MockTransport(fake.handler))
    audit = AuditLog.open(paths)
    engine = PolicyEngine(cfg, audit=audit)
    tracker = ProvenanceTracker()
    hub = McpHub(cfg, engine, LocalLauncher(cfg, srt=None), PinStore.at(paths.data_dir), audit,
                 provenance=tracker, backends=backends)  # fmt: skip
    app = FastAPI()
    add_mcp_routes(app, hub, lambda r: ("jan", None), default_environment=lambda c: "lit",
                   run_environment=lambda r: None)  # fmt: skip
    client = httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://127.0.0.1")
    return client, hub, audit, tracker


async def rpc(c: httpx.AsyncClient, method: str, params: dict[str, Any] | None = None) -> Any:
    resp = await c.post("/mcp", json={"jsonrpc": "2.0", "id": 1, "method": method,
                                      "params": params or {}})  # fmt: skip
    return resp.json()


async def test_prompts_and_resources(paths: Paths) -> None:
    c, hub, audit, _ = await setup(paths)
    try:
        init = await rpc(
            c,
            "initialize",
            {
                "protocolVersion": "2025-06-18",
                "capabilities": {},
                "clientInfo": {"name": "test", "version": "1"},
            },
        )
        assert {"tools", "prompts", "resources"} <= set(init["result"]["capabilities"])
        prompts = (await rpc(c, "prompts/list"))["result"]["prompts"]
        assert [p["name"] for p in prompts] == ["apa-review"]  # "other" isn't enabled here
        got = (await rpc(c, "prompts/get", {"name": "apa-review"}))["result"]
        text = got["messages"][0]["content"]["text"]
        assert text.startswith("Check every") and "skill://apa-review/examples.md" in text
        assert (await rpc(c, "prompts/get", {"name": "other"}))["error"]["code"] == -32602
        res = (await rpc(c, "resources/list"))["result"]["resources"]
        assert [r["uri"] for r in res] == ["skill://apa-review/examples.md"]
        read = await rpc(c, "resources/read", {"uri": "skill://apa-review/examples.md"})
        assert read["result"]["contents"][0]["text"].startswith("Smith")
        bad = await rpc(c, "resources/read", {"uri": "skill://apa-review/escape.txt"})
        assert bad["error"]["code"] == -32002

    finally:
        await c.aclose()
        await hub.aclose()
    actions = [(r.action, r.phase, r.target) for r in audit.records()]
    assert ("skill.use", "event", "apa-review") in actions
    audit.close()


async def test_artifact_tools(paths: Paths) -> None:
    (paths.config_dir / "environments" / "lit.toml").write_text(
        'tools = ["artifacts"]\n[privacy]\nisolation = "off"\n'
    )
    c, hub, audit, tracker = await setup(paths)
    try:
        assert "error" not in await rpc(c, "tools/list")
        no_conv = await rpc(c, "tools/call", {"name": "cairnward__artifact_list", "arguments": {}})
        assert no_conv["result"]["isError"]
        tracker.note_conversation("jan", None, "conv-1")
        w = await rpc(
            c,
            "tools/call",
            {
                "name": "cairnward__artifact_write",
                "arguments": {"path": "plan.md", "content": "step 1"},
            },
        )
        assert "saved plan.md" in w["result"]["content"][0]["text"]
        r = await rpc(
            c, "tools/call", {"name": "cairnward__artifact_read", "arguments": {"path": "plan.md"}}
        )
        assert r["result"]["content"][0]["text"] == "step 1"
        bad = await rpc(
            c,
            "tools/call",
            {"name": "cairnward__artifact_write", "arguments": {"path": "../x", "content": "y"}},
        )
        assert bad["result"]["isError"]
    finally:
        await c.aclose()
        await hub.aclose()
    assert any(
        x.action == "tool.call" and x.target == "cairnward/artifact_write" and x.phase == "post"
        for x in audit.records()
    )
    audit.close()


@pytest.mark.skipif(shutil.which("git") is None, reason="needs git")
async def test_artifact_tools_follow_the_conversations_lock(paths: Paths) -> None:
    """A model can't read or write a locked conversation's artifacts (FR-ENC-1), through the
    real tool route, and the failure is audited without any content."""
    from cairnward.store import Vault

    (paths.config_dir / "environments" / "lit.toml").write_text(
        'tools = ["artifacts"]\n[privacy]\nisolation = "off"\n'
    )
    secret = "correct horse battery staple 42"
    c, hub, audit, tracker = await setup(paths)
    vault = Vault.open(paths)
    hub.vault = vault

    async def call(tool: str, **arguments: str) -> dict[str, Any]:
        reply = await rpc(c, "tools/call", {"name": f"cairnward__{tool}", "arguments": arguments})
        return dict(reply["result"])

    try:
        cid = vault.create_conversation(title="t")
        tracker.note_conversation("jan", None, cid)
        assert "saved" in (await call("artifact_write", path="plan.md", content="private"))[
            "content"][0]["text"]  # fmt: skip
        vault.lock(cid, secret)
        for tool, args in (("artifact_read", {"path": "plan.md"}), ("artifact_list", {}),
                           ("artifact_write", {"path": "new.md", "content": "x"})):  # fmt: skip
            out = await call(tool, **args)
            assert out["isError"] and "locked" in out["content"][0]["text"]
            assert "private" not in str(out)
        vault.unlock(cid, secret, minutes=5)
        assert (await call("artifact_read", path="plan.md"))["content"][0]["text"] == "private"
        await call("artifact_write", path="new.md", content="x")
        assert (await call("artifact_list"))["content"][0]["text"] == "new.md\nplan.md"
    finally:
        await c.aclose()
        await hub.aclose()
        vault.close()
    assert not any("private" in r.to_json_line() for r in audit.records())
    audit.close()
