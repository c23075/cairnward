"""lit.ask: answers from a library with page citations (FR-RES-2), with a scripted model."""

import json
from pathlib import Path
from typing import Any

import httpx
import pytest

from cairnward.audit import AuditLog
from cairnward.backends import Backends
from cairnward.config import load_config
from cairnward.config.paths import Paths
from cairnward.core.management import management_app
from cairnward.gateway.clients import RunKeys
from cairnward.gateway.scheduling import Scheduler
from cairnward.policy import PolicyEngine
from cairnward.research import InProcessEmbedder, Research
from cairnward.research.ask import cited_numbers, fit, run_ask
from cairnward.research.library import Hit
from tests.fakes.backends import FakeOllama
from tests.fakes.pdf import make_pdf

QUESTION = "Does longer sleep improve working memory?"


def test_cited_numbers() -> None:
    dash = chr(0x2013)  # en dash, as models often write ranges
    text = f"a [1]. b [2, 3] c [4{dash}6] d [2] e [10-11]"
    assert cited_numbers(text) == [1, 2, 3, 4, 5, 6, 10, 11]
    assert cited_numbers("no citations (see 2020)") == []


def hit(n: int, words: int) -> Hit:
    return Hit(n, n, f"T{n}", f"/p/{n}.pdf", 1, 1, "word " * words, 1.0 / n, n, None)


def test_fit_keeps_best_ranked_within_context() -> None:
    hits = [hit(i, 400) for i in range(1, 9)]  # ~500 tokens each
    assert [h.chunk_id for h in fit(hits, 4096, "q")] == [1, 2, 3, 4]
    assert len(fit(hits, None, "q")) == 8


@pytest.fixture
async def env(tmp_path: Path) -> Any:
    paths = Paths(tmp_path / "cfg", tmp_path / "data", tmp_path / "state")
    cfg = load_config(paths)
    fake = FakeOllama()
    backends = Backends(cfg.backends, transport_for=lambda n: httpx.MockTransport(fake.handler))
    audit = AuditLog.open(paths)
    engine = PolicyEngine(cfg, audit=audit)
    papers = tmp_path / "papers"
    papers.mkdir()
    (papers / "sleep.pdf").write_bytes(make_pdf(
        [[f"Line {i}: participants who slept longer had better working memory." for i in range(25)],
         [f"Line {i}: the effect held after controlling for age." for i in range(25)]],
        title="Sleep and Memory"))  # fmt: skip
    (papers / "caffeine.pdf").write_bytes(make_pdf(
        [[f"Line {i}: caffeine shortened reaction time." for i in range(25)]],
        title="Caffeine Trial"))  # fmt: skip
    with Research.open(paths) as r:
        r.create("thesis", papers, "litreview-offline")
        await r.index("thesis", InProcessEmbedder(cfg, engine, backends,
                                                  environment="litreview-offline"))  # fmt: skip
    yield cfg, engine, backends, fake, audit
    audit.close()
    await backends.aclose()


async def test_answer_with_sources(env: Any) -> None:
    cfg, engine, backends, fake, audit = env
    fake.reply = (
        "<think>let me see</think>Yes: longer sleep went with better working memory [1, 2]. "
        "Some say otherwise [9]."
    )
    answer = await run_ask(cfg, engine, backends, "thesis", QUESTION, k=4,
                           scheduler=Scheduler())  # fmt: skip
    assert answer.passages == 4 and not answer.uncited and not answer.cannot_answer
    assert [s.n for s in answer.sources] == [1, 2]
    assert answer.invalid_citations == [9] and "[?]" in answer.text and "<think>" not in answer.text
    sent = fake.requests[-1]["messages"]
    assert sent[0]["role"] == "system" and "ONLY the numbered passages" in sent[0]["content"]
    assert "[1] " in sent[1]["content"] and sent[1]["content"].endswith(f"Question: {QUESTION}")
    rendered = answer.render()
    assert "Sources:" in rendered and ", p. " in rendered and "removed citations" in rendered
    recs = audit.records()
    (pre,) = [
        r for r in recs if r.action == "model.call" and r.details.get("operation") == "lit.ask"
    ]
    assert any(r.phase == "post" and r.pre_seq == pre.seq and r.outcome == "ok" for r in recs)
    (event,) = [r for r in recs if r.action == "research.ask"]
    assert event.details["sources"] == 2 and QUESTION not in json.dumps([r.details for r in recs])


async def test_cannot_answer_and_uncited(env: Any) -> None:
    cfg, engine, backends, fake, _ = env
    fake.reply = "I can't answer this from these sources. They don't cover diet."
    a = await run_ask(cfg, engine, backends, "thesis", "What about diet and memory?")
    assert a.cannot_answer and a.uncited and "Warning" not in a.render()
    fake.reply = "Sleep is good for memory."
    b = await run_ask(cfg, engine, backends, "thesis", QUESTION)
    assert b.uncited and not b.cannot_answer and "don't rely on it" in b.render()


async def test_management_endpoint(env: Any) -> None:
    cfg, engine, backends, fake, audit = env
    fake.reply = "Yes [1]."

    async def research_ask(library: str, question: str, **kw: Any) -> Any:
        return await run_ask(cfg, engine, backends, library, question, **kw)

    app = management_app(cfg, audit=audit, run_keys=RunKeys(), egress=None, isolated=False,
                         research_ask=research_ask)  # fmt: skip
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app),
                                 base_url="http://cairnward") as c:  # fmt: skip
        ok = await c.post("/v1/research/ask", json={"library": "thesis", "question": QUESTION})
        assert ok.status_code == 200 and ok.json()["sources"][0]["title"] == "Sleep and Memory"
        bad = await c.post("/v1/research/ask", json={"library": "nope", "question": QUESTION})
        assert bad.status_code == 400
