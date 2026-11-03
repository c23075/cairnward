"""lit.ask: answers from a library with page citations (FR-RES-2), with a scripted model."""

import json
from pathlib import Path
from types import SimpleNamespace
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
from cairnward.research.ask import (
    ANSWER_TOKENS,
    REASONING_TOKENS,
    cited_numbers,
    fit,
    mark_invalid,
    run_ask,
)
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


def test_mark_invalid() -> None:
    assert mark_invalid("a [1] b [1, 2] c [1-2].", 2) == "a [1] b [1, 2] c [1-2]."
    text = "a [9] b [1, 9] c [1-3] d [8, 9]."
    assert mark_invalid(text, 2) == "a [?] b [1, ?] c [1, 2, ?] d [?]."


async def test_reasoning_that_uses_the_whole_limit_is_reported(env: Any) -> None:
    """The shape of a real reply (q1 in evals/pdf_qa): Ollama returns reasoning, empty
    content and done_reason "length"."""
    cfg, engine, backends, fake, audit = env
    fake.reply, fake.thinking, fake.done_reason = "", "Let me check the passages...", "length"
    answer = await run_ask(cfg, engine, backends, "thesis", QUESTION, k=4)
    assert answer.cut_off and answer.text == "" and answer.sources == []
    rendered = answer.render()
    assert "reached its token limit before answering" in rendered
    assert "cites no passage" not in rendered
    (rec,) = [r for r in audit.records() if r.action == "research.ask"]
    assert rec.details["cut_off"] is True


async def test_answer_cut_off_after_it_started(env: Any) -> None:
    cfg, engine, backends, fake, _ = env
    fake.reply, fake.done_reason = "Longer sleep went with better memory [1] and", "length"
    answer = await run_ask(cfg, engine, backends, "thesis", QUESTION, k=4)
    assert answer.cut_off and [s.n for s in answer.sources] == [1]
    assert "this answer is cut off" in answer.render()


async def test_unfinished_think_tag_is_not_taken_as_the_answer(env: Any) -> None:
    """Servers that keep reasoning in the content: a cut-off reply has no closing tag, and
    numbers in the reasoning must not count as citations."""
    cfg, engine, backends, fake, _ = env
    fake.reply, fake.done_reason = "<think>Passage [1] says...", "length"
    answer = await run_ask(cfg, engine, backends, "thesis", QUESTION, k=4)
    assert answer.cut_off and answer.text == "" and answer.sources == []


async def test_limit_leaves_room_for_reasoning_only_when_it_is_on(env: Any) -> None:
    cfg, engine, backends, fake, _ = env
    await run_ask(cfg, engine, backends, "thesis", QUESTION, k=4, effort="high")
    assert fake.requests[-1]["think"] is True
    assert fake.requests[-1]["options"]["num_predict"] == ANSWER_TOKENS + REASONING_TOKENS
    await run_ask(cfg, engine, backends, "thesis", QUESTION, k=4, effort="medium")
    assert fake.requests[-1]["think"] is False
    assert fake.requests[-1]["options"]["num_predict"] == ANSWER_TOKENS


def test_reply_room_comes_out_of_the_passages() -> None:
    hits = [SimpleNamespace(text="word " * 1000, chunk_id=i) for i in range(4)]
    roomy = fit(hits, 8192, "q")  # type: ignore[arg-type]
    tight = fit(hits, 8192, "q", reply_tokens=5000)  # type: ignore[arg-type]
    assert len(tight) < len(roomy)


async def test_reasoning_with_thinking_off_is_not_taken_as_the_answer(env: Any) -> None:
    """The shape of a real reply (qwen3:30b with think=false): reasoning in the content,
    no opening tag, then a closing one. Numbers in it must not count as citations."""
    cfg, engine, backends, fake, _ = env
    fake.reply = "We are given passages. Passage [3] looks useful.\n</think>\n\nYes, it helps [1]."
    answer = await run_ask(cfg, engine, backends, "thesis", QUESTION, k=4)
    assert answer.text == "Yes, it helps [1]." and [s.n for s in answer.sources] == [1]


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
