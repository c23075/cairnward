"""PaperQA2 deep engine (FR-RES-13): the guard that refuses any setting not pointing at
Cairnward, and the mapping of its answers. Runs without paper-qa installed."""

import importlib.util
import pickle
from pathlib import Path
from types import ModuleType
from typing import Any

import pytest

from cairnward.research.engines import paperqa

GW = "http://127.0.0.1:8484/v1"


def worker() -> ModuleType:
    spec = importlib.util.spec_from_file_location("pqa_worker", paperqa.WORKER)
    assert spec and spec.loader
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def cfg(model: str) -> dict[str, Any]:
    return {
        "model_list": [{"model_name": model, "litellm_params": {"model": model, "api_base": GW}}]
    }


def good() -> dict[str, Any]:
    m = "openai/cw/e:medium"
    return {
        "llm": m, "llm_config": cfg(m), "summary_llm": m, "summary_llm_config": cfg(m),
        "embedding": "openai/cw/e:embed", "embedding_config": {"kwargs": {"api_base": GW}},
        "parsing": {"use_doc_details": False, "multimodal": 0, "enrichment_llm": m,
                    "enrichment_llm_config": cfg(m)},
        "agent": {"agent_llm": m, "agent_llm_config": cfg(m)},
    }  # fmt: skip


def test_model_calls_allow_for_a_slow_machine() -> None:
    """The library's 60 s default failed on the reference machine (an answer takes minutes)."""
    w = worker()
    (entry,) = w.llm_config("openai/cw/e", GW, "key")["model_list"]
    assert entry["litellm_params"]["timeout"] >= 1800
    assert entry["litellm_params"]["max_retries"] == 0
    assert entry["litellm_params"]["api_base"] == GW
    assert w.guard({**good(), "llm_config": w.llm_config("openai/cw/e", GW, "key")}, GW) == []


def test_guard_accepts_cairnward_only() -> None:
    w = worker()
    assert w.guard(good(), GW) == []
    bad = good()
    bad["agent"]["agent_llm"] = "gpt-4o"  # a default left in place
    assert any("agent.agent_llm" in p for p in w.guard(bad, GW))
    bad = good()
    bad["summary_llm_config"]["model_list"][0]["litellm_params"]["api_base"] = (
        "https://api.openai.com/v1"
    )
    problems = w.guard(bad, GW)
    assert any("not the gateway" in p for p in problems)
    assert any("points outside Cairnward" in p for p in problems)
    bad = good()
    bad["parsing"]["use_doc_details"] = True
    assert any("use_doc_details" in p for p in w.guard(bad, GW))
    bad = good()
    bad["parsing"]["multimodal"] = 1
    assert any("multimodal" in p for p in w.guard(bad, GW))
    bad = good()
    bad["somewhere"] = {"url": "https://api.semanticscholar.org/graph"}
    assert any("points outside" in p for p in w.guard(bad, GW))
    bad = good()
    del bad["embedding_config"]
    assert any("embedding_config" in p for p in w.guard(bad, GW))


class Row:
    def __init__(self, text: str):
        self.text, self.document_id, self.title, self.path = text, 1, "Sleep", "s.pdf"
        self.page_start = self.page_end = 2


def test_answer_mapping() -> None:
    papers = {
        "sleep": {"title": "Sleep and Memory", "file": "sleep.pdf", "document_id": 7},
        "caffeine": {"title": "Caffeine Trial", "file": "caffeine.pdf", "document_id": 8},
    }
    result = {"answer": 'Sleep helped "longer sleep improved memory" (sleep pages 2-3, '
                        "caffeine pages 1-1). Caffeine also (caffeine pages 1-1).",
              "contexts": [{"name": "sleep pages 2-3", "score": 8},
                           {"name": "caffeine pages 1-1", "score": 6},
                           {"name": "sleep pages 4-4", "score": 0}]}  # fmt: skip
    a = paperqa.to_answer("q", "lib", result, papers, [Row("x longer sleep improved memory y")])
    assert a.text == 'Sleep helped "longer sleep improved memory" [1, 2]. Caffeine also [2].'
    assert [(s.n, s.file, s.pages, s.document_id) for s in a.sources] == [
        (1, "sleep.pdf", "2-3", 7), (2, "caffeine.pdf", "1", 8)]  # fmt: skip
    assert a.passages == 3 and not a.uncited and a.quote_problems == []
    c = paperqa.to_answer("q", "lib", {"answer": "I cannot answer.", "contexts": []}, papers, [])
    assert c.cannot_answer and c.uncited


def test_index_cache_is_loaded_only_when_signed(tmp_path: Path) -> None:
    """The index is a pickle (loading it runs code): only Cairnward's signed copy is read."""
    w = worker()
    key, cache = bytes(32), tmp_path / "index.pkl"
    assert w.load_cache(cache, key) is None  # nothing yet
    w.save_cache(cache, key, {"docs": ["a"]})
    assert w.load_cache(cache, key) == {"docs": ["a"]}
    assert w.load_cache(cache, b"x" * 32) is None  # another key

    class Boom:
        def __reduce__(self) -> Any:
            return (exec, ("raise SystemExit('pickle ran')",))

    cache.write_bytes(pickle.dumps(Boom()))  # planted, old signature left in place
    assert w.load_cache(cache, key) is None
    (tmp_path / "index.pkl.sig").unlink()
    assert w.load_cache(cache, key) is None  # unsigned


def test_cache_key_is_kept_owner_only(tmp_path: Path) -> None:
    from cairnward.config.paths import Paths

    paths = Paths(tmp_path / "c", tmp_path / "d", tmp_path / "s")
    first = paperqa.cache_key(paths)
    assert len(first) == 32 and paperqa.cache_key(paths) == first
    assert (paths.state_dir / "paperqa-cache.key").stat().st_mode & 0o077 == 0


@pytest.mark.parametrize(
    ("text", "declines"),
    [
        ("I cannot answer. The context discusses diversity patterns.", True),
        ("I can\u2019t answer this.", True),
        # real answers from the eval's unanswerable question
        ("The context does not identify any specific bee species [1].", True),
        ("The context provides no information on the most common bee species.", True),
        ("Therefore, it is impossible to determine the most common bee species [1].", True),
        ("Therefore, the question cannot be answered from the given information.", True),
        # real answers to questions it could answer
        ("Six gardeners reported using pesticides [1].", False),
        ("The study does not recommend lawns; it recommends native plants [1].", False),
    ],
)
def test_refusals_in_other_words(text: str, declines: bool) -> None:
    assert paperqa.to_answer(
        "q", "lib", {"answer": text, "contexts": []}, {}, []
    ).cannot_answer is (declines)


def test_prompt_additions_are_safe_to_format() -> None:
    mod = worker()
    assert mod.SUMMARY_RULES.format() == mod.SUMMARY_RULES  # PaperQA2 formats its prompts
    assert mod.ANSWER_RULES.format() == mod.ANSWER_RULES
    assert "I cannot answer." in mod.ANSWER_RULES  # the phrase PaperQA2 itself looks for
