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
    """Answers like the models would: screening JSON, notes JSON, synthesis prose. Papers
    whose title contains a word in `exclude` are excluded at screening."""

    context_tokens = 8192
    thinking = False

    def __init__(self, crash_on: int | None = None, exclude: tuple[str, ...] = ()):
        self.calls, self.crash_on, self.exclude = 0, crash_on, exclude
        self.kinds: list[str] = []
        self.efforts: list[str | None] = []

    async def ask(self, messages: list[dict[str, Any]], *, max_tokens: int = 700) -> Reply:
        self.calls += 1
        if self.crash_on == self.calls:
            raise asyncio.CancelledError  # the server is stopped mid-step
        system, user = messages[0]["content"], messages[-1]["content"]
        if system.startswith("You screen papers"):
            self.kinds.append("screen")
            out = any(w in user.split("Title:", 1)[1].split("\n")[0] for w in self.exclude)
            text = json.dumps(
                {
                    "decision": "exclude" if out else "include",
                    "reason": "off topic" if out else "about sleep and memory",
                }
            )
            return Reply(text, "fast", 50, 20, 0.01)
        if system.startswith("You write the synthesis"):
            self.kinds.append("synthesis")
            return Reply("Longer sleep went with better memory [1, 2]; one study disagrees [9].",
                         "main", 300, 80, 0.01)  # fmt: skip
        self.kinds.append("notes")
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
                     ask=ask or no_ask, chat_for=lambda lib, effort=None: chat)  # fmt: skip


async def test_review_survives_a_restart(lib: Paths) -> None:
    job = review_job("thesis", "Does sleep help memory?", screen=False)
    first = runner(lib, FakeChat(crash_on=2))
    first.submit(job)
    with pytest.raises(asyncio.CancelledError):
        await first.run(first.store.load(job.id))
    saved = first.store.load(job.id)
    assert saved.status == "running" and [s.status for s in saved.steps] == [
        "done", "done", "pending", "pending", "pending", "pending"]  # fmt: skip

    chat = FakeChat()
    second = runner(lib, chat)  # a fresh process after the restart
    nxt = second._next()
    assert nxt is not None and nxt.id == job.id
    done = await second.run(nxt)
    assert done.status == "done" and chat.kinds == ["notes", "notes", "synthesis"]  # A not again
    result = second.store.result_path(job.id).read_text()
    assert all(f"| Paper {x} |" in result for x in "ABC") and "Small \\| sample (p. 2)" in result
    assert "## References" in result and "*Paper A*" in result
    import shutil

    from cairnward.artifacts import ArtifactStore

    if shutil.which("git"):  # artifacts need git; the result file doesn't
        assert ArtifactStore(lib.data_dir).read("job", job.id, "result.md").decode() == result


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
            assert job["total"] == 1 and job["status"] == "queued"  # papers picked at start
            await r.run(r.store.load(job["id"]))
            shown = (await c.get(f"/v1/jobs/{job['id']}")).json()
            assert shown["status"] == "done" and shown["result"].endswith("result.md")
            names = [s["name"] for s in shown["steps"]]
            assert names == ["prepare", *["screen"] * 3, "select", *["notes"] * 3, "synthesis",
                             "table"]  # fmt: skip
            assert [s["status"] for s in shown["steps"]] == ["done"] * 10
            assert (
                await c.post("/v1/jobs", json={"kind": "ask", "library": "thesis"})
            ).status_code == 400
            outside = {"kind": "review", "library": "thesis", "question": "q",
                       "files": [str(lib.data_dir / "x.pdf")]}  # fmt: skip
            resp = await c.post("/v1/jobs", json=outside)
            assert resp.status_code == 400 and "isn't in the library folder" in resp.text
            later = {"kind": "review", "library": "thesis", "question": "q",
                     "files": ["a.pdf", "new/d.pdf"]}  # fmt: skip
            assert (await c.post("/v1/jobs", json=later)).json()["not_found"] == ["new/d.pdf"]
            unknown = {"kind": "review", "library": "x", "question": "q"}
            assert (await c.post("/v1/jobs", json=unknown)).status_code == 404
            assert (await c.get("/v1/jobs/zzz")).status_code == 404
        assert {"job.submit", "job.finish"} <= {x.action for x in audit.records()}


async def test_review_picks_papers_when_it_starts(lib: Paths, tmp_path: Path) -> None:
    """Files added after queuing are indexed and reviewed; --files limits the review."""
    from cairnward.research.jobs import library_files

    calls: list[tuple[str, list[str] | None, str]] = []

    async def index(library: str, files: list[str] | None, actor: str) -> dict[str, Any]:
        calls.append((library, files, actor))
        with Research.open(lib) as r:
            embedder = HashEmbedder(model_name="cw/litreview-offline:embed")
            report = await r.index(library, embedder, only=files)
        return {"added": report.added, "missing": report.missing}

    folder = tmp_path / "papers"
    r = runner(lib, FakeChat())
    r.index = index
    later = (datetime.now(UTC) + timedelta(hours=1)).isoformat(timespec="seconds")
    whole = r.submit(review_job("thesis", "Sleep?", not_before=later))
    chosen = r.submit(review_job("thesis", "Sleep?", files=library_files(
        folder, [str(folder / "new" / "d.pdf"), "a.pdf", "gone.pdf"])))  # fmt: skip
    (folder / "new").mkdir()  # added after both were queued
    (folder / "new" / "d.pdf").write_bytes(make_pdf(
        [[f"Paper d line {i}: sleep." for i in range(20)]] * 2, title="Paper D"))  # fmt: skip

    done = await r.run(r.store.load(chosen.id))
    assert calls[0] == ("thesis", ["new/d.pdf", "a.pdf", "gone.pdf"], f"job:{chosen.id}")
    notes = [s.params["title"] for s in done.steps if s.name == "notes"]
    assert notes == ["Paper D", "Paper A"] and done.steps[-1].name == "table"
    assert done.steps[0].output["skipped"] == [{"file": "gone.pdf", "why": "not found"}]
    result = r.store.result_path(chosen.id).read_text()
    assert "| Paper D |" in result and "| Paper B |" not in result
    assert "Not reviewed: gone.pdf (not found)" in result
    with Research.open(lib) as rs:  # the other papers weren't touched (not marked missing)
        assert {d.path: d.status for d in rs.documents("thesis")}["b.pdf"] == "indexed"

    done = await r.run(r.store.load(whole.id))
    assert calls[1] == ("thesis", None, f"job:{whole.id}")
    assert len([s for s in done.steps if s.name == "notes"]) == 4  # a, b, c and new/d

    empty = r.submit(review_job("thesis", "Sleep?", files=["gone.pdf"]))
    failed = await r.run(r.store.load(empty.id))
    assert failed.status == "failed" and "no indexed papers" in (failed.steps[0].error or "")


def test_library_files(tmp_path: Path) -> None:
    from cairnward.research.jobs import library_files

    folder = tmp_path / "papers"
    folder.mkdir()
    assert library_files(folder, ["a.pdf", str(folder / "sub" / "b.PDF"), "a.pdf"]) == [
        "a.pdf", "sub/b.PDF"]  # fmt: skip
    for bad, why in ((["../x.pdf"], "isn't in the library folder"), (["a.txt"], "isn't a PDF"),
                     ([], "no files")):  # fmt: skip
        with pytest.raises(ValueError, match=why):
            library_files(folder, bad)


async def test_review_that_excludes_everything_says_so(lib: Paths, tmp_path: Path) -> None:
    """Otherwise a job that read nothing looks like a finished review."""
    chat = FakeChat(exclude=("Paper",))

    async def no_ask(*a: Any, **k: Any) -> None:
        raise AssertionError("not used")

    r = JobRunner(JobStore(lib.data_dir), research=lambda: Research.open(lib), ask=no_ask,
                  chat_for=lambda library, effort=None: chat)  # fmt: skip
    job = r.submit(review_job("thesis", "Does sleep help working memory?"))
    done = await r.run(r.store.load(job.id))
    assert done.status == "done" and "notes" not in chat.kinds
    select = next(s for s in done.steps if s.name == "select")
    assert select.output["included"] == 0 and "excluded every paper" in select.output["warning"]
    result = r.store.result_path(job.id).read_text()
    assert "**Check:** screening excluded every paper" in result and "--no-screen" in result


def test_reply_room_includes_reasoning_only_when_it_is_on() -> None:
    from types import SimpleNamespace

    from cairnward.research.ask import REASONING_TOKENS
    from cairnward.research.jobs import _reply_tokens

    assert _reply_tokens(SimpleNamespace(thinking=False), 150) == 150
    assert _reply_tokens(SimpleNamespace(thinking=True), 150) == 150 + REASONING_TOKENS


async def test_full_review_pipeline(lib: Paths, tmp_path: Path) -> None:
    """screen → select → notes on included papers only, all with the main model →
    synthesis with checked citations → table, references and .bib (FR-RES-4, D63)."""
    folder = tmp_path / "papers"
    abstract = ("This paper reports a cohort of 40 older adults whose sleep was tracked for a "
                "month; longer sleep went with better working memory.")  # fmt: skip
    (folder / "a.pdf.csl.json").write_text(json.dumps({
        "type": "article-journal", "title": "Paper A", "abstract": abstract,
        "author": [{"family": "Walker", "given": "M."}], "issued": {"date-parts": [[2022]]},
        "DOI": "10.1/a"}))  # fmt: skip
    chat = FakeChat(exclude=("Paper C",))
    efforts: list[str | None] = []
    prompts: list[str] = []

    def chat_for(library: str, effort: str | None = None) -> FakeChat:
        efforts.append(effort)
        return chat

    original = chat.ask

    async def ask(messages: list[dict[str, Any]], *, max_tokens: int = 700) -> Reply:
        prompts.append(messages[-1]["content"])
        return await original(messages, max_tokens=max_tokens)

    chat.ask = ask  # type: ignore[method-assign]

    async def no_ask(*a: Any, **k: Any) -> None:
        raise AssertionError("not used")

    r = JobRunner(JobStore(lib.data_dir), research=lambda: Research.open(lib),
                  ask=no_ask, chat_for=chat_for)  # fmt: skip
    job = r.submit(review_job("thesis", "Does sleep help working memory?",
                              criteria="adults; sleep measured"))  # fmt: skip
    assert job.eta() == 30  # only "prepare" planned so far
    done = await r.run(r.store.load(job.id))
    assert done.status == "done"
    kinds = [s.name for s in done.steps]
    assert kinds == ["prepare", "screen", "screen", "screen", "select", "notes", "notes",
                     "synthesis", "table"]  # fmt: skip
    # all screening before any reading; both use the environment's default effort (D63)
    assert chat.kinds == ["screen"] * 3 + ["notes"] * 2 + ["synthesis"]
    assert set(efforts) == {None}
    assert abstract in prompts[0] and "Criteria: adults; sleep measured" in prompts[0]
    select = next(s for s in done.steps if s.name == "select")
    assert select.output == {"included": 2, "excluded": 1, "unscreened": 0}
    synth = next(s for s in done.steps if s.name == "synthesis").output
    assert synth["invalid_citations"] == [9] and synth["uncited"] == []
    result = r.store.result_path(job.id).read_text()
    assert "## Screening" in result and "| Paper C | exclude | off topic |" in result
    assert "## Synthesis" in result and "cites papers that don't exist: [9]" in result
    assert "| Paper C |" not in result.split("## Comparison")[1]
    bib = (r.store.result_path(job.id).parent / "references.bib").read_text()
    assert "@article{Walker2022paper," in bib and "doi = {10.1/a}" in bib  # from the sidecar
    assert bib.count("@article") == 2


def test_eta_uses_step_kinds() -> None:
    from cairnward.research.jobs import Job, Step

    steps = [Step("screen", {}, "done", seconds=8), Step("screen", {}, "done", seconds=12),
             Step("screen", {}), Step("select", {})]  # fmt: skip
    job = Job("abc123abc123", "review", {}, steps)
    # one more screen (10 s), select (1 s), and 3 papers to read at the 420 s prior + synthesis
    assert job.eta() == 10 + 1 + 3 * 420 + 300  # the finished screens' average, not the prior


def test_parsers() -> None:
    from cairnward.research.jobs import citations_in, parse_screen

    assert parse_screen('{"decision": "Exclude", "reason": "animals"}') == {
        "decision": "exclude", "reason": "animals"}  # fmt: skip
    assert parse_screen("I would exclude this")["decision"] == "exclude"
    assert parse_screen("hmm")["decision"] == "unsure"
    assert citations_in("a [2] b [1, 3] c [2-4] d [5\u20136] [x]") == [2, 1, 3, 2, 3, 4, 5, 6]
