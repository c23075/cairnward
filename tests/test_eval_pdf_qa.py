"""The lit.ask eval's plumbing (not model quality): papers are built so that each
fact's passage is on its own page, and answers are scored as intended."""

import importlib.util
import json
import subprocess
from pathlib import Path
from types import ModuleType

import pytest

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


def test_save_answers(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    ev = load()
    answer = {"text": "It retreated 1.8 km [1].", "seconds": 3,
              "sources": [{"file": "glacier.pdf", "pages": "p. 2"}]}  # fmt: skip

    def fake_cw(*args: str) -> subprocess.CompletedProcess[str]:
        return subprocess.CompletedProcess(args, 0, json.dumps(answer), "")

    monkeypatch.setattr(ev, "cw", fake_cw)
    saved = tmp_path / "answers.jsonl"
    for _ in range(2):  # appends, so repeated runs keep every answer
        ev.run(only=["q1"], emit=lambda _line: None, save_answers=saved)
    rows = [json.loads(line) for line in saved.read_text().splitlines()]
    assert len(rows) == 2 and rows[0]["id"] == "q1" and rows[0]["pass"]
    assert rows[0]["answer"]["text"] == answer["text"]


def test_scoring() -> None:
    ev = load()
    q = {"id": "q", "expect": {"file": "a.pdf", "page": 2, "keywords": ["1.8"]}}
    good = {"text": "It retreated 1.8 km [1].", "sources": [{"file": "a.pdf", "pages": "p. 2"}],
            "invalid_citations": [], "seconds": 3}  # fmt: skip
    assert ev.score(q, good)["pass"]
    wrong_page = {**good, "sources": [{"file": "a.pdf", "pages": "pp. 3-4"}]}
    assert not ev.score(q, wrong_page)["pass"]
    spanning = {**good, "sources": [{"file": "a.pdf", "pages": "pp. 1-3"}]}
    assert ev.score(q, spanning)["pass"]
    invented = {**good, "invalid_citations": [7]}
    assert not ev.score(q, invented)["pass"]
    u = {"id": "u", "expect": {"unanswerable": True}}
    assert ev.score(u, {"cannot_answer": True})["pass"]
    assert not ev.score(u, {"cannot_answer": False})["pass"]


def test_a_fact_can_be_written_in_words() -> None:
    ev = load()
    q = {"id": "q", "expect": {"file": "a.pdf", "page": 3, "keywords": [["6", "six"]]}}
    sources = [{"file": "a.pdf", "pages": "p. 3"}]
    for text, expected in (("Six gardeners [1].", True), ("6 gardeners [1].", True),
                           ("Five gardeners [1].", False)):  # fmt: skip
        assert ev.score(q, {"text": text, "sources": sources})["content"] is expected


def test_every_question_scores(tmp_path: Path) -> None:
    ev = load()
    for q in json.loads((EVAL / "questions.json").read_text())["questions"]:
        assert "pass" in ev.score(q, {"text": "", "sources": []})
