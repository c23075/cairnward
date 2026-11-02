"""PaperQA2 in research jobs (FR-RES-13 + FR-RES-3): the core issues and revokes the run
key, maps and records the answer; the worker runs wherever sandboxes may start."""

from pathlib import Path
from typing import Any

import httpx
import pytest

from cairnward.audit import AuditLog
from cairnward.config import load_config
from cairnward.config.paths import Paths
from cairnward.egress import Sessions
from cairnward.egress.control import control_app
from cairnward.gateway.clients import RunKeys
from cairnward.research import Research
from cairnward.research.engines.deep import DeepEngine
from cairnward.research.engines.paperqa import EngineError
from cairnward.research.jobs import JobRunner, JobStore, ask_job
from tests.fakes.pdf import HashEmbedder, make_pdf


@pytest.fixture
async def lib(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Paths:
    paths = Paths(tmp_path / "cfg", tmp_path / "data", tmp_path / "state")
    monkeypatch.setenv("CAIRNWARD_CACHE_DIR", str(tmp_path / "cache"))
    exe = paths.data_dir / "engines" / "paperqa" / "venv" / "bin" / "python"
    exe.parent.mkdir(parents=True)
    exe.write_text("")  # "installed"
    papers = tmp_path / "papers"
    papers.mkdir()
    (papers / "sleep.pdf").write_bytes(make_pdf([["Sleep helps memory."] * 20], title="Sleep"))
    with Research.open(paths) as r:
        r.create("thesis", papers, "litreview-offline")
        await r.index("thesis", HashEmbedder(model_name="cw/litreview-offline:embed"))
    return paths


class FakeExecutor:
    def __init__(self, fail: bool = False):
        self.calls: list[dict[str, Any]] = []
        self.fail = fail

    async def __call__(self, library: str, question: str, **kw: Any) -> dict[str, Any]:
        self.calls.append({"library": library, "question": question, **kw})
        if self.fail:
            raise EngineError("worker failed")
        return {"answer": "Yes (sleep pages 1-1).",
                "contexts": [{"name": "sleep pages 1-1", "score": 8}]}  # fmt: skip


async def test_run_key_lifecycle_and_audit(lib: Paths) -> None:
    cfg = load_config(lib)
    keys, ex = RunKeys(), FakeExecutor()
    with AuditLog.open(lib) as audit:
        deep = DeepEngine(cfg, run_keys=keys, executor=ex, audit=audit)
        answer = await deep.ask("thesis", "Does sleep help?", actor="job:abc")
        assert answer.sources[0].file == "sleep.pdf" and answer.text == "Yes [1]."
        call = ex.calls[0]
        assert call["gateway_port"] == cfg.main.server.gateway_port
        assert keys.identify(call["key"]) is None  # revoked after the run
        recs = audit.records()
        start = next(r for r in recs if r.action == "run.start")
        assert start.client == "paperqa" and start.actor == "job:abc"
        assert any(r.action == "run.end" and r.outcome == "ok" and r.run_id == start.run_id
                   for r in recs)  # fmt: skip
        assert any(r.action == "research.ask" and r.details["engine"] == "paperqa" for r in recs)
        failing = DeepEngine(cfg, run_keys=keys, executor=FakeExecutor(fail=True), audit=audit)
        with pytest.raises(EngineError):
            await failing.ask("thesis", "q?")
        assert audit.records()[-1].action == "run.end" and audit.records()[-1].outcome == "error"


async def test_jobs_use_the_deep_engine(lib: Paths) -> None:
    cfg = load_config(lib)
    ex = FakeExecutor()
    deep = DeepEngine(cfg, run_keys=RunKeys(), executor=ex)

    async def lean(*a: Any, **k: Any) -> Any:
        raise AssertionError("the lean engine shouldn't be used")

    runner = JobRunner(JobStore(lib.data_dir), research=lambda: Research.open(lib), ask=lean,
                       chat_for=lambda library: None, deep=deep)  # fmt: skip
    job = runner.submit(ask_job("thesis", ["Does sleep help?", "Why?"], engine="paperqa",
                                skip_summary=True))  # fmt: skip
    assert job.summary()["engine"] == "paperqa"
    done = await runner.run(job)
    assert done.status == "done" and len(ex.calls) == 2 and ex.calls[0]["skip_summary"] is True
    assert "Yes [1]." in runner.store.result_path(job.id).read_text()


async def test_outer_process_endpoint() -> None:
    seen: list[dict[str, Any]] = []

    async def deep(**kw: Any) -> dict[str, Any]:
        seen.append(kw)
        if kw["library"] == "bad":
            raise EngineError("no such library")
        return {"answer": "ok", "contexts": []}

    app = control_app(Sessions(), deep=deep)
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app),
                                 base_url="http://egress") as c:  # fmt: skip
        body = {"library": "thesis", "question": "Why?", "key": "cw-run-x", "gateway_port": 8484}
        assert (await c.post("/v1/engines/paperqa/run", json=body)).json()["answer"] == "ok"
        assert seen[0]["evidence_k"] == 10 and seen[0]["key"] == "cw-run-x"
        bad = await c.post("/v1/engines/paperqa/run", json={**body, "library": "bad"})
        assert bad.status_code == 400 and "no such library" in bad.text
    none = control_app(Sessions())
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=none),
                                 base_url="http://egress") as c:  # fmt: skip
        assert (await c.post("/v1/engines/paperqa/run", json=body)).status_code == 404
