"""Skills served as MCP prompts/resources (FR-MCP-4) and research search as an MCP tool
(FR-RES-10, lit.search part)."""

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
from cairnward.research import InProcessEmbedder, Research
from tests.fakes.backends import FakeOllama
from tests.fakes.pdf import make_pdf

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
    fake.reply = "Longer sleep went with better working memory [1]."

    async def research_ask(library: str, question: str, **kw: Any) -> Any:
        from cairnward.research.ask import run_ask

        return await run_ask(cfg, engine, backends, library, question, **kw)

    hub = McpHub(cfg, engine, LocalLauncher(cfg, srt=None), PinStore.at(paths.data_dir), audit,
                 provenance=tracker, backends=backends, research_ask=research_ask)  # fmt: skip
    from cairnward.research.jobs import JobRunner, JobStore

    hub.jobs = JobRunner(JobStore(paths.data_dir), research=lambda: Research.open(paths),
                         ask=research_ask, chat_for=lambda lib: None)  # fmt: skip
    app = FastAPI()
    add_mcp_routes(app, hub, lambda r: ("jan", None), default_environment=lambda c: "lit",
                   run_environment=lambda r: None)  # fmt: skip
    client = httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://127.0.0.1")
    # an indexed library in this environment
    papers = paths.config_dir.parent / "papers"
    papers.mkdir()
    (papers / "sleep.pdf").write_bytes(make_pdf(
        [[f"Sleep line {i}: longer sleep improved working memory." for i in range(30)]] * 2,
        title="Sleep and Memory"))  # fmt: skip
    with Research.open(paths) as r:
        r.create("thesis", papers, "lit")
        await r.index("thesis", InProcessEmbedder(cfg, engine, backends, environment="lit"))
    return client, hub, audit, tracker


async def rpc(c: httpx.AsyncClient, method: str, params: dict[str, Any] | None = None) -> Any:
    resp = await c.post("/mcp", json={"jsonrpc": "2.0", "id": 1, "method": method,
                                      "params": params or {}})  # fmt: skip
    return resp.json()


async def test_prompts_resources_and_lit_search(paths: Paths) -> None:
    c, hub, audit, tracker = await setup(paths)
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

        tools = (await rpc(c, "tools/list"))["result"]["tools"]
        assert [t["name"] for t in tools] == [
            "cairnward__lit_search",
            "cairnward__lit_review_start",
            "cairnward__lit_ask",
        ]
        lit = tools[0]
        queued = await rpc(
            c,
            "tools/call",
            {"name": "cairnward__lit_review_start", "arguments": {"question": "Does sleep help?"}},
        )
        job_id = queued["result"]["structuredContent"]["id"]
        assert hub.jobs.store.load(job_id).status == "queued"
        assert "cairnward jobs show" in queued["result"]["content"][0]["text"]
        answer = await rpc(
            c,
            "tools/call",
            {"name": "cairnward__lit_ask", "arguments": {"question": "Does sleep help?"}},
        )
        assert answer["result"]["structuredContent"]["sources"][0]["title"] == "Sleep and Memory"
        assert "Sources:" in answer["result"]["content"][0]["text"]
        assert lit["inputSchema"]["properties"]["library"]["enum"] == ["thesis"]
        hit = (
            await rpc(
                c,
                "tools/call",
                {"name": "cairnward__lit_search", "arguments": {"query": "working memory"}},
            )
        )["result"]
        assert not hit.get("isError")
        assert hit["structuredContent"]["hits"][0]["title"] == "Sleep and Memory"
        assert "[Sleep and Memory, p. " in hit["content"][0]["text"]
        assert tracker.current("jan", None)[0] == ("output of lit_ask in thesis",
                                                   "output of lit_search in thesis")  # fmt: skip
    finally:
        await c.aclose()
        await hub.aclose()
    actions = [(r.action, r.phase, r.target) for r in audit.records()]
    assert ("skill.use", "event", "apa-review") in actions
    assert ("tool.call", "post", "cairnward/lit_search") in actions
    audit.close()


async def test_research_tool_needs_opt_in(paths: Paths) -> None:
    (paths.config_dir / "environments" / "lit.toml").write_text('[privacy]\nisolation = "off"\n')
    c, hub, audit, _ = await setup(paths)
    try:
        assert (await rpc(c, "tools/list"))["result"]["tools"] == []
        res = await rpc(c, "tools/call", {"name": "cairnward__lit_search",
                                          "arguments": {"query": "x"}})  # fmt: skip
        assert "error" in res
    finally:
        await c.aclose()
        await hub.aclose()
        audit.close()


@pytest.mark.skipif(shutil.which("git") is None, reason="needs git")
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
