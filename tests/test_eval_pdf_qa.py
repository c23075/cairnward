"""The lit.ask eval's plumbing (not model quality): papers are built so that each
fact's passage is on its own page, and answers are scored as intended."""

import importlib.util
import json
from pathlib import Path
from types import ModuleType

EVAL = Path(__file__).parent.parent / "evals" / "pdf_qa"


def load() -> ModuleType:
    spec = importlib.util.spec_from_file_location("pdf_qa_eval", EVAL / "run_eval.py")
    assert spec and spec.loader
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def test_facts_land_on_their_pages(tmp_path: Path) -> None:
    from cairnward.research import chunk_pages, read_pdf

    ev = load()
    spec = json.loads((EVAL / "questions.json").read_text())
    ev.build(tmp_path, spec)
    for name, paper in spec["papers"].items():
        chunks = chunk_pages(read_pdf(tmp_path / name).pages)
        for page_no, fact in enumerate(paper["pages"], 1):
            probe = " ".join(fact.split()[:8])
            holders = [c for c in chunks if probe in " ".join(c.text.split())]
            assert any(c.page_start == c.page_end == page_no for c in holders), (name, page_no)


def test_scoring() -> None:
    ev = load()
    q = {"id": "q", "expect": {"file": "a.pdf", "page": 2, "keywords": ["1.8"]}}
    good = {"text": "It retreated 1.8 km [1].", "sources": [{"file": "a.pdf", "pages": "2"}],
            "invalid_citations": [], "seconds": 3}  # fmt: skip
    assert ev.score(q, good)["pass"]
    wrong_page = {**good, "sources": [{"file": "a.pdf", "pages": "3-4"}]}
    assert not ev.score(q, wrong_page)["pass"]
    invented = {**good, "invalid_citations": [7]}
    assert not ev.score(q, invented)["pass"]
    u = {"id": "u", "expect": {"unanswerable": True}}
    assert ev.score(u, {"cannot_answer": True})["pass"]
    assert not ev.score(u, {"cannot_answer": False})["pass"]
