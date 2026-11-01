"""Research jobs (FR-RES-3): persistent, resumable after a restart, schedulable; the
review job is the M1 exit criterion ("a review job survives a restart mid-run")."""

import asyncio
import json
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import httpx
import pytest

from cairnward.audit import AuditLog
from cairnward.cli import parse_at
from cairnward.config import load_config
from cairnward.config.paths import Paths
from cairnward.core.management import management_app
from cairnward.gateway.clients import RunKeys
from cairnward.research import Research
from cairnward.research.jobs import JobRunner, JobStore, ask_job, parse_notes, review_job
from cairnward.research.llm import Reply
from tests.fakes.pdf import HashEmbedder, make_pdf

NOTES = json.dumps({
    "aim": "Test sleep (p. 1)", "method": "Cohort (p. 1)", "sample": "n=40 (p. 1)",
    "findings": "Better memory (p. 2)", "limitations": "Small | sample (p. 2)",
})  # fmt: skip


class FakeChat:
    context_tokens = 8192

    def __init__(self, crash_on: int | None = None):
        self.calls, self.crash_on = 0, crash_on

    async def ask(self, messages: list[dict[str, Any]], *, max_tokens: int = 700) -> Reply:
        self.calls += 1
        if self.crash_on == self.calls:
            raise asyncio.CancelledError  # the server is stopped mid-step
        return Reply(NOTES, "fake", 100, 50, 0.01)


@pytest.fixture
async def lib(tmp_path: Path) -> Paths:
    paths = Paths(tmp_path / "cfg", tmp_path / "data", tmp_path / "state")
    papers = tmp_path / "papers"
    papers.mkdir()
    for name in ("a", "b", "c"):
        (papers / f"{name}.pdf").write_bytes(make_pdf(
            [[f"Paper {name} line {i}: sleep and memory." for i in range(20)]] * 2,
            title=f"Paper {name.upper()}"))  # fmt: skip
    with Research.open(paths) as r:
        r.create("thesis", papers, "litreview-offline")
        await r.index("thesis", HashEmbedder(model_name="cw/litreview-offline:embed"))
    return paths


def runner(paths: Paths, chat: FakeChat, ask: Any = None) -> JobRunner:
    async def no_ask(*a: Any, **k: Any) -> Any:
        raise AssertionError("not used")

    return JobRunner(JobStore(paths.data_dir), research=lambda: Research.open(paths),
                     ask=ask or no_ask, chat_for=lambda lib: chat)  # fmt: skip


async def test_review_survives_a_restart(lib: Paths) -> None:
    with Research.open(lib) as r:
        job = review_job("thesis", "Does sleep help memory?", r.documents("thesis"))
    first = runner(lib, FakeChat(crash_on=2))
    first.submit(job)
    with pytest.raises(asyncio.CancelledError):
        await first.run(first.store.load(job.id))
    saved = first.store.load(job.id)
    assert saved.status == "running" and [s.status for s in saved.steps] == [
        "done", "pending", "pending", "pending"]  # fmt: skip

    chat = FakeChat()
    second = runner(lib, chat)  # a fresh process after the restart
    nxt = second._next()
    assert nxt is not None and nxt.id == job.id
    done = await second.run(nxt)
    assert done.status == "done" and chat.calls == 2  # paper A wasn't read again
    result = second.store.result_path(job.id).read_text()
    assert all(f"| Paper {x} |" in result for x in "ABC") and "Small \\| sample (p. 2)" in result
    assert "## References" in result and "*Paper A*" in result


async def test_schedule_cancel_and_ask_jobs(lib: Paths) -> None:
    class A:
        def as_dict(self) -> dict[str, Any]:
            return {"question": "q", "library": "thesis", "text": "Yes [1].", "sources": [
                {"n": 1, "title": "Paper A", "file": "a.pdf", "pages": "1", "document_id": 1,
                 "chunk_id": 1}], "passages": 3}  # fmt: skip

    async def fake_ask(library: str, question: str, **kw: Any) -> Any:
        return A()

    r = runner(lib, FakeChat(), ask=fake_ask)
    later = (datetime.now(UTC) + timedelta(hours=2)).isoformat(timespec="seconds")
    scheduled = r.submit(ask_job("thesis", ["q1", "q2"], not_before=later))
    assert r._next() is None  # not yet
    assert r.cancel(scheduled.id) and not r.cancel(scheduled.id)
    now_job = r.submit(ask_job("thesis", ["Does sleep help?", "", "Why?"]))
    done = await r.run(r._next() or now_job)
    assert done.status == "done" and len(done.steps) == 2
    assert "Yes [1]." in r.store.result_path(done.id).read_text()
    assert [j.status for j in r.store.all()] == ["cancelled", "done"]


def test_parse_notes_and_at() -> None:
    assert parse_notes(f"Here you go:\n```json\n{NOTES}\n```")["sample"] == "n=40 (p. 1)"
    assert parse_notes("no json")["raw"] == "no json"
    base = datetime(2026, 11, 1, 22, 0).astimezone()
    assert datetime.fromisoformat(parse_at("23:30", base)) == base.replace(hour=23, minute=30)
    assert datetime.fromisoformat(parse_at("21:00", base)) == (base + timedelta(days=1)).replace(
        hour=21, minute=0)  # fmt: skip


async def test_management_api(lib: Paths) -> None:
    cfg = load_config(lib)
    r = runner(lib, FakeChat())
    with AuditLog.open(lib) as audit:
        r.audit = audit
        app = management_app(cfg, audit=audit, run_keys=RunKeys(), egress=None, isolated=False,
                             jobs=r)  # fmt: skip
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app),
                                     base_url="http://cairnward") as c:  # fmt: skip
            job = (await c.post("/v1/jobs", json={"kind": "review", "library": "thesis",
                                                  "question": "Sleep?"})).json()  # fmt: skip
            assert job["total"] == 4 and job["status"] == "queued"
            await r.run(r.store.load(job["id"]))
            shown = (await c.get(f"/v1/jobs/{job['id']}")).json()
            assert shown["status"] == "done" and shown["result"].endswith("result.md")
            assert [s["status"] for s in shown["steps"]] == ["done"] * 4
            assert (
                await c.post("/v1/jobs", json={"kind": "ask", "library": "thesis"})
            ).status_code == 400
            unknown = {"kind": "review", "library": "x", "question": "q"}
            assert (await c.post("/v1/jobs", json=unknown)).status_code == 404
            assert (await c.get("/v1/jobs/zzz")).status_code == 404
        assert {"job.submit", "job.finish"} <= {x.action for x in audit.records()}
