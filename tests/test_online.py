"""Online sources inside Cairnward's rules (FR-RES-8/9): opt-in per environment, every host
checked by the policy engine (allow-list, ask, off), queries scanned before they leave,
audited by digest; the management endpoints. Mock transport: nothing leaves the machine."""

import base64
import json
from pathlib import Path

import httpx
import pytest

from cairnward.audit import AuditLog
from cairnward.backends import Backends
from cairnward.config import load_config
from cairnward.config.paths import Paths
from cairnward.core.management import management_app
from cairnward.gateway.clients import RunKeys
from cairnward.policy import ApprovalAnswer, ApprovalRequest, PolicyEngine, ScriptedApprover
from cairnward.research.online import Online, OnlineError
from tests.test_sources import PDF, responder


@pytest.fixture
def paths(tmp_path: Path) -> Paths:
    p = Paths(tmp_path / "cfg", tmp_path / "data", tmp_path / "state")
    (p.config_dir / "environments").mkdir(parents=True)
    (p.config_dir / "environments" / "closed.toml").write_text(
        'research_sources = ["openalex"]\n[privacy]\nnetwork = "off"\n'
    )
    (p.config_dir / "cairnward.toml").write_text('[privacy]\nredaction = "enforce"\n')
    return p


def online(paths: Paths, audit: AuditLog, asked: list[str], calls: list[str]) -> Online:
    cfg = load_config(paths)

    def answer(req: ApprovalRequest) -> ApprovalAnswer:
        asked.append(req.target)
        return ApprovalAnswer(False)  # the user says no to unlisted hosts

    engine = PolicyEngine(cfg, approver=ScriptedApprover(answer), audit=audit)
    return Online(cfg, engine, Backends(cfg.backends), transport=responder(calls))


async def test_search_and_fetch_follow_the_environment(paths: Paths) -> None:
    asked: list[str] = []
    calls: list[str] = []
    with AuditLog.open(paths) as audit:
        o = online(paths, audit, asked, calls)
        out = await o.search("litreview-online", "sleep and working memory", limit=5)
        assert out["records"] and not asked  # the five API hosts are on its allow-list
        assert all("error" not in r for r in out["sources"].values())
        # the PDF host isn't listed: asked, the user said no, nothing downloaded
        items = await o.fetch("litreview-online", ["doi:10.1111/jsr.99001"])
        assert asked == ["example-repository.org:443"] and "refused" in items[0]["error"]
        assert not any("example-repository" in c for c in calls)
        # an environment with network off: every source refused before any request
        n = len(calls)
        closed = await o.search("closed", "sleep and working memory")
        assert "refused" in closed["sources"]["openalex"]["error"] and len(calls) == n
        with pytest.raises(OnlineError, match="online sources are off"):
            await o.search("litreview-offline", "sleep and working memory")
        with pytest.raises(OnlineError, match="not enabled"):
            await o.search("closed", "sleep", sources=["arxiv"])
        # a query carrying a secret never leaves (redaction = enforce)
        with pytest.raises(OnlineError, match="not sent"):
            await o.search("litreview-online", "papers AKIAIOSFODNN7EXAMPLE leaked keys")
        recs = audit.records()
        searches = [r for r in recs if r.action == "research.search"]
        assert searches and "query_digest" in searches[0].details
        assert "working memory" not in json.dumps([r.details for r in recs])
        assert any(r.action == "net.connect" and r.decision == "deny" for r in recs)


async def test_fetch_when_allowed(paths: Paths) -> None:
    (paths.config_dir / "environments" / "open.toml").write_text(
        'research_sources = ["openalex"]\n[privacy]\nnetwork = "open"\n'
    )
    with AuditLog.open(paths) as audit:
        o = online(paths, audit, [], [])
        (item,) = await o.fetch("open", ["10.1111/jsr.99001"])
        assert base64.b64decode(item["pdf_b64"]) == PDF
        assert item["filename"].endswith(".pdf") and item["csl"]["DOI"] == "10.1111/jsr.99001"


async def test_management_endpoints(paths: Paths, tmp_path: Path) -> None:
    from cairnward.research import Research

    papers = tmp_path / "papers"
    papers.mkdir()
    with Research.open(paths) as r:
        r.create("thesis", papers, "litreview-online")
    with AuditLog.open(paths) as audit:
        o = online(paths, audit, [], [])
        app = management_app(o.cfg, audit=audit, run_keys=RunKeys(), egress=None,
                             isolated=False, online=o)  # fmt: skip
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app),
                                     base_url="http://cairnward") as c:  # fmt: skip
            found = (
                await c.post(
                    "/v1/research/search", json={"query": "sleep memory", "library": "thesis"}
                )
            ).json()
            assert found["environment"] == "litreview-online" and found["records"]
            bad = await c.post(
                "/v1/research/search", json={"query": "sleep", "environment": "litreview-offline"}
            )
            assert bad.status_code == 400 and "off" in bad.json()["detail"]
            missing = await c.post("/v1/research/fetch", json={"library": "nope", "ids": ["x"]})
            assert missing.status_code == 404
