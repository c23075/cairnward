"""The citation-existence eval's plumbing, on the real checker and real PDFs. The cases are
the point: what it reports here is what the eval reports with the real embedding model."""

import importlib.util
import json
from pathlib import Path
from types import ModuleType
from typing import Any

import httpx
import pytest

from cairnward.audit import AuditLog
from cairnward.backends import Backends
from cairnward.config import load_config
from cairnward.config.paths import Paths
from cairnward.policy import PolicyEngine
from cairnward.research import InProcessEmbedder, Research
from cairnward.research.check import check_draft
from tests.fakes.backends import FakeOllama
from tests.fakes.pdf import make_pdf

EVAL = Path(__file__).parent.parent / "evals" / "citation_existence"


def load() -> ModuleType:
    spec = importlib.util.spec_from_file_location("citation_eval", EVAL / "run_eval.py")
    assert spec and spec.loader
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


@pytest.fixture
def cases() -> dict[str, Any]:
    data: dict[str, Any] = json.loads((EVAL / "cases.json").read_text())
    return data


async def run_cases(tmp_path: Path, cases: dict[str, Any], text: str) -> dict[str, Any]:
    paths = Paths(tmp_path / "cfg", tmp_path / "data", tmp_path / "state")
    cfg = load_config(paths)
    fake = FakeOllama()
    backends = Backends(cfg.backends, transport_for=lambda n: httpx.MockTransport(fake.handler))
    audit = AuditLog.open(paths)
    engine = PolicyEngine(cfg, audit=audit)
    folder = tmp_path / "papers"
    folder.mkdir()
    for file, paper in cases["papers"].items():
        (folder / file).write_bytes(make_pdf([[p] for p in paper["pages"]], title=paper["title"]))
    try:
        with Research.open(paths) as r:
            r.create("lib", folder, "litreview-offline")
            await r.index("lib", InProcessEmbedder(cfg, engine, backends,
                                                   environment="litreview-offline"))  # fmt: skip
            report: dict[str, Any] = check_draft(r, "lib", text).as_dict()
    finally:
        audit.close()
        await backends.aclose()
    return report


async def test_every_case_gets_its_known_verdict(tmp_path: Path, cases: dict[str, Any]) -> None:
    ev = load()
    rows = ev.score(cases, await run_cases(tmp_path, cases, ev.draft(cases)))
    assert len(rows) == len(cases["quotes"]) + len(cases["references"])
    assert [r for r in rows if not r["pass"]] == []
    assert not any(r["missed"] for r in rows)


def test_a_miss_is_counted_as_a_miss(cases: dict[str, Any]) -> None:
    """If the checker let everything through, the eval has to say so."""
    ev = load()
    fooled = {
        "quotes": [
            {"quote": q["text"], "status": "elsewhere", "ok": True} for q in cases["quotes"]
        ],
        "references": [{"entry": r["text"], "status": "in_library"} for r in cases["references"]],
    }
    rows = ev.score(cases, fooled)
    missed = {r["id"] for r in rows if r["missed"]}
    assert {"quote:invented", "quote:reworded", "quote:changed-number",
            "reference:invented", "reference:similar-title"} <= missed  # fmt: skip
    assert not next(r for r in rows if r["id"] == "quote:invented")["pass"]


def test_an_omitted_case_fails(cases: dict[str, Any]) -> None:
    rows = load().score(cases, {"quotes": [], "references": []})
    assert not any(r["pass"] for r in rows)
