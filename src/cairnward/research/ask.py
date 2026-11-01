"""`lit.ask`: answer a question from a literature library, citing pages (FR-RES-2).

Cairnward's lean pipeline (ADR-0014): hybrid search picks the passages, the model
answers only from them and cites each claim as [n]; citations are mapped back to the
document and pages, and checked:
- a citation to a passage that wasn't given is removed from the sources and reported;
- an answer without any citation is marked "uncited" (not trustworthy);
- "I can't answer from these sources" is a valid answer.
Passages are kept within the model's context (lowest-ranked dropped first).
"""

from __future__ import annotations

import re
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any

from cairnward.gateway.scheduling import estimate_tokens
from cairnward.research.embedder import Embedder
from cairnward.research.library import Hit, Research
from cairnward.research.llm import InProcessChat

SYSTEM = (
    "You answer questions about research papers using ONLY the numbered passages the user "
    "gives you. Rules:\n"
    "1. Cite every claim with the passage number in square brackets, like [2] or [1, 3].\n"
    "2. Cite only passages that support the claim; never cite a number that wasn't given.\n"
    "3. If the passages don't contain the answer, reply exactly: "
    '"I can\'t answer this from these sources." and explain briefly what is missing.\n'
    "4. Quote sparingly and exactly, in double quotes, with a citation.\n"
    "5. Be concise. Do not add a reference list; the numbers are enough."
)
CANT = "i can't answer this from these sources"
_CITE = re.compile(r"\[(\d+(?:\s*(?:,|\u2013|-|\u2014)\s*\d+)*)\]")
ANSWER_TOKENS = 1024
OVERHEAD_TOKENS = 400


@dataclass(frozen=True)
class Source:
    n: int
    title: str
    file: str
    pages: str
    document_id: int
    chunk_id: int


@dataclass
class Answer:
    question: str
    library: str
    text: str
    sources: list[Source] = field(default_factory=list)  # cited passages, in citation order
    passages: int = 0  # passages given to the model
    invalid_citations: list[int] = field(default_factory=list)
    cannot_answer: bool = False
    uncited: bool = False
    model: str = ""
    prompt_tokens: int | None = None
    completion_tokens: int | None = None
    seconds: float = 0.0

    def as_dict(self) -> dict[str, Any]:
        return asdict(self)

    def render(self) -> str:
        lines = [self.text, ""]
        if self.sources:
            lines.append("Sources:")
            lines += [f"[{s.n}] {s.title}, p. {s.pages} ({s.file})" for s in self.sources]
        if self.invalid_citations:
            lines.append("Note: removed citations to passages that weren't given: "
                         + ", ".join(f"[{n}]" for n in self.invalid_citations))  # fmt: skip
        if self.uncited and not self.cannot_answer:
            lines.append("Warning: the answer cites no passage; don't rely on it.")
        return "\n".join(lines).rstrip() + "\n"


def cited_numbers(text: str) -> list[int]:
    """Passage numbers cited in the text, in first-mention order; ranges expanded."""
    seen: list[int] = []
    for group in _CITE.findall(text):
        for part in re.split(r"\s*,\s*", group):
            bounds = re.split(r"\s*[\u2013\-\u2014]\s*", part)
            if len(bounds) == 2 and bounds[0].isdigit() and bounds[1].isdigit():
                lo, hi = int(bounds[0]), int(bounds[1])
                nums = list(range(lo, hi + 1)) if 0 < hi - lo < 20 else [lo, hi]
            else:
                nums = [int(b) for b in bounds if b.isdigit()]
            for n in nums:
                if n not in seen:
                    seen.append(n)
    return seen


def fit(hits: list[Hit], context: int | None, question: str) -> list[Hit]:
    """Keep the best-ranked passages that fit in the context with room for the answer."""
    if not context:
        return hits
    budget = context - ANSWER_TOKENS - OVERHEAD_TOKENS - estimate_tokens(SYSTEM + question)
    kept, used = [], 0
    for h in hits:
        cost = estimate_tokens(h.text) + 20
        if used + cost > budget:
            break
        kept.append(h)
        used += cost
    return kept


def build_messages(question: str, hits: list[Hit]) -> list[dict[str, str]]:
    blocks = []
    for n, h in enumerate(hits, 1):
        title = h.title or Path(h.path).name
        blocks.append(f"[{n}] {title}, p. {h.pages}\n{h.text.strip()}")
    user = "Passages:\n\n" + "\n\n".join(blocks) + f"\n\nQuestion: {question}"
    return [{"role": "system", "content": SYSTEM}, {"role": "user", "content": user}]


async def ask(
    research: Research,
    library: str,
    question: str,
    *,
    embedder: Embedder,
    chat: InProcessChat,
    k: int = 8,
) -> Answer:
    question = question.strip()
    hits = fit(await research.search(library, question, embedder, k=k), chat.context_tokens,
               question)  # fmt: skip
    answer = Answer(question=question, library=library, text="", passages=len(hits))
    if not hits:
        answer.text = "I can't answer this from these sources: nothing relevant was found."
        answer.cannot_answer = True
        return answer
    reply = await chat.ask(build_messages(question, hits), max_tokens=ANSWER_TOKENS)
    answer.model, answer.seconds = reply.model, reply.seconds
    answer.prompt_tokens, answer.completion_tokens = reply.prompt_tokens, reply.completion_tokens
    text = reply.text
    answer.cannot_answer = CANT in text.lower().replace("\u2019", "'")
    for n in cited_numbers(text):
        if 1 <= n <= len(hits):
            h = hits[n - 1]
            answer.sources.append(Source(n, h.title or Path(h.path).name, Path(h.path).name,
                                         h.pages, h.document_id, h.chunk_id))  # fmt: skip
        else:
            answer.invalid_citations.append(n)
    for n in answer.invalid_citations:  # don't leave dangling numbers in the text
        text = re.sub(rf"\[{n}\]", "[?]", text)
    answer.text = text
    answer.uncited = not answer.sources
    return answer


async def run_ask(
    cfg: Any,
    engine: Any,
    backends: Any,
    library: str,
    question: str,
    *,
    k: int = 8,
    effort: str | None = None,
    scheduler: Any = None,
    actor: str = "cairnward",
) -> Answer:
    """Open the library, embed and answer with the library's environment's models; the
    question itself is recorded in the audit log only as a digest."""
    from cairnward.audit.log import digest
    from cairnward.research.embedder import InProcessEmbedder

    research = Research.open(cfg.paths)
    try:
        lib = research.get(library)
        embedder = InProcessEmbedder(cfg, engine, backends, environment=lib.environment)
        chat = InProcessChat(cfg, engine, backends, environment=lib.environment, effort=effort,
                             scheduler=scheduler, actor=actor, purpose="lit.ask")  # fmt: skip
        answer = await ask(research, library, question, embedder=embedder, chat=chat, k=k)
    finally:
        research.close()
    if engine.audit:
        engine.audit.append("research.ask", actor=actor, target=library,
                            environment=lib.environment, outcome="ok", details={
                                "question_digest": digest(question), "passages": answer.passages,
                                "sources": len(answer.sources),
                                "invalid_citations": len(answer.invalid_citations),
                                "cannot_answer": answer.cannot_answer, "uncited": answer.uncited,
                                "seconds": round(answer.seconds, 1)})  # fmt: skip
    return answer
