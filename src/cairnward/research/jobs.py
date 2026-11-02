"""Research jobs: queued, persistent, resumable, schedulable (spec §4.6, FR-RES-3).

A job is a list of steps saved to `<data>/jobs/<id>/job.json` after every step (written
atomically), so a crash or restart loses at most the step in progress; the runner picks
up from the first unfinished step. The runner lives in the core, runs one job at a time
(one CPU), and its model calls wait in the gateway's per-backend queue like any client.

Job types:
- `ask`: lit.ask for each question in a list.
- `review` (first slice of the review pipeline, FR-RES-4): when it starts, the library
  (or the chosen files) is indexed and the papers picked; then for each paper, the main
  model reads it (as much as fits the context) and writes structured notes with page
  numbers; then a comparison table and a bibliography are assembled without a model.
Results are Markdown in `<data>/jobs/<id>/result.md`.
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import os
import re
import secrets
import time
from collections.abc import Awaitable, Callable
from dataclasses import asdict, dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Literal

from cairnward.gateway.scheduling import estimate_tokens

Status = Literal["queued", "running", "done", "failed", "cancelled"]
StepStatus = Literal["pending", "done", "failed"]
NOTE_FIELDS = ("aim", "method", "sample", "findings", "limitations")


_last_created = [datetime.min.replace(tzinfo=UTC)]


def _now() -> str:
    return datetime.now(UTC).isoformat(timespec="microseconds")


def _created() -> str:
    """Strictly increasing, so jobs keep their order even with a coarse clock (Windows)."""
    from datetime import timedelta

    now = max(datetime.now(UTC), _last_created[0] + timedelta(microseconds=1))
    _last_created[0] = now
    return now.isoformat(timespec="microseconds")


@dataclass
class Step:
    name: str
    params: dict[str, Any]
    status: StepStatus = "pending"
    output: Any = None
    error: str | None = None
    seconds: float = 0.0


@dataclass
class Job:
    id: str
    kind: Literal["ask", "review"]
    params: dict[str, Any]
    steps: list[Step]
    status: Status = "queued"
    created: str = field(default_factory=_created)
    not_before: str | None = None  # ISO time: scheduled ("run overnight")
    started: str | None = None
    finished: str | None = None
    error: str | None = None
    actor: str = "user"

    @property
    def progress(self) -> tuple[int, int]:
        return sum(s.status != "pending" for s in self.steps), len(self.steps)

    def summary(self) -> dict[str, Any]:
        done, total = self.progress
        avg = [s.seconds for s in self.steps if s.status == "done" and s.seconds]
        eta = round(sum(avg) / len(avg) * (total - done)) if avg and done < total else None
        return {"id": self.id, "kind": self.kind, "status": self.status, "done": done,
                "total": total, "created": self.created, "not_before": self.not_before,
                "finished": self.finished, "eta_s": eta, "error": self.error,
                "library": self.params.get("library"),
                "engine": self.params.get("engine", "lean")}  # fmt: skip


class JobStore:
    def __init__(self, data_dir: Path):
        self.root = data_dir / "jobs"

    def _dir(self, job_id: str) -> Path:
        if not re.fullmatch(r"[0-9a-f]{12}", job_id):
            raise KeyError(job_id)
        return self.root / job_id

    def save(self, job: Job) -> None:
        d = self._dir(job.id)
        d.mkdir(parents=True, exist_ok=True)
        if os.name != "nt":
            self.root.chmod(0o700)
        tmp = d / "job.json.tmp"
        tmp.write_text(json.dumps(asdict(job), indent=1, ensure_ascii=False), encoding="utf-8")
        tmp.replace(d / "job.json")

    def load(self, job_id: str) -> Job:
        data = json.loads((self._dir(job_id) / "job.json").read_text(encoding="utf-8"))
        data["steps"] = [Step(**s) for s in data["steps"]]
        return Job(**data)

    def all(self) -> list[Job]:
        if not self.root.is_dir():
            return []
        jobs = []
        for d in sorted(self.root.iterdir()):
            with contextlib.suppress(OSError, ValueError, KeyError, TypeError):
                jobs.append(self.load(d.name))
        return sorted(jobs, key=lambda j: j.created)

    def write_result(self, job: Job, text: str) -> Path:
        path = self._dir(job.id) / "result.md"
        path.write_text(text, encoding="utf-8")
        return path

    def result_path(self, job_id: str) -> Path:
        return self._dir(job_id) / "result.md"


def new_job(kind: Literal["ask", "review"], params: dict[str, Any], steps: list[Step], *,
            not_before: str | None = None, actor: str = "user") -> Job:  # fmt: skip
    return Job(secrets.token_hex(6), kind, params, steps, not_before=not_before, actor=actor)


def ask_job(library: str, questions: list[str], *, engine: str = "lean",
            evidence_k: int = 10, skip_summary: bool = False, **kw: Any) -> Job:  # fmt: skip
    qs = [q.strip() for q in questions if q.strip()]
    steps = [Step("ask", {"question": q}) for q in qs]
    params: dict[str, Any] = {"library": library, "questions": len(qs), "engine": engine}
    if engine == "paperqa":
        params |= {"evidence_k": evidence_k, "skip_summary": skip_summary}
    return new_job("ask", params, steps, **kw)


def review_job(library: str, question: str, style: str = "apa", *,
               files: list[str] | None = None, **kw: Any) -> Job:  # fmt: skip
    """A review whose papers are picked when it starts: the first step indexes the library
    (or just `files`, paths relative to its folder) and then adds one notes step per
    paper and the table, so PDFs added before a scheduled start are included."""
    params = {"library": library, "question": question, "style": style}
    prepare = Step("prepare", {"title": "index and pick the papers", "files": files})
    return new_job("review", params, [prepare], **kw)


def library_files(folder: Path, given: list[str]) -> list[str]:
    """Paths (absolute, or relative to the library folder) -> PDF paths relative to it."""
    root = folder.resolve()
    out: list[str] = []
    for raw in given:
        p = Path(raw).expanduser()
        p = (p if p.is_absolute() else root / p).resolve()
        try:
            rel = p.relative_to(root).as_posix()
        except ValueError:
            raise ValueError(f"{raw} isn't in the library folder {folder}: copy it there "
                             "first") from None  # fmt: skip
        if p.suffix.lower() != ".pdf":
            raise ValueError(f"{raw} isn't a PDF")
        if rel not in out:
            out.append(rel)
    if not out:
        raise ValueError("no files given")
    return out


# ------------------------------------------------------------------ step work

NOTES_SYSTEM = (
    "You read one research paper and write notes for a literature review. Use only the "
    "paper's text. Reply with a JSON object with these keys: aim, method, sample, findings, "
    "limitations. Each value is one or two sentences ending with the page, like (p. 4). "
    'Write "not reported" when the paper doesn\'t say.'
)


def _paper_text(rows: list[Any], budget_tokens: int) -> str:
    """The paper's chunks in order, without the overlaps, within the budget."""
    out, used = [], 0
    for r in rows:
        cost = estimate_tokens(r.text)
        if used + cost > budget_tokens:
            break
        out.append(f"[p. {r.page_start}] {r.text.strip()}")
        used += cost
    return "\n\n".join(out)


def parse_notes(text: str) -> dict[str, str]:
    m = re.search(r"\{.*\}", text, re.DOTALL)
    if m:
        with contextlib.suppress(ValueError):
            data = json.loads(m.group(0))
            if isinstance(data, dict):
                return {k: str(data.get(k, "not reported")).strip() for k in NOTE_FIELDS}
    return {k: "" for k in NOTE_FIELDS} | {"raw": text.strip()[:4000]}


# ------------------------------------------------------------------ runner


Ask = Callable[..., Awaitable[Any]]  # (library, question, **kw) -> Answer
# (library, files or None, actor) -> index report as a dict
Index = Callable[[str, list[str] | None, str], Awaitable[dict[str, Any]]]
Chat = Callable[[list[dict[str, Any]]], Awaitable[Any]]  # messages -> research.llm.Reply


class JobRunner:
    """Runs queued jobs one at a time, saving after each step."""

    def __init__(self, store: JobStore, *, research: Callable[[], Any], ask: Ask,
                 chat_for: Callable[[str], Any], audit: Any = None,
                 log: Callable[[str], None] = lambda m: None, deep: Any = None,
                 index: Index | None = None, cite_engine: str = "builtin",
                 paths: Any = None):  # fmt: skip
        self.store, self.research, self.ask, self.chat_for = store, research, ask, chat_for
        self.index = index  # indexes a library (or some of its files) before a review
        self.cite_engine, self.paths = cite_engine, paths  # reference formatting (research.csl)
        self.deep = deep  # research.engines.deep.DeepEngine (PaperQA2), optional
        self.audit, self.log = audit, log
        self.wake = asyncio.Event()
        self._cancelled: set[str] = set()

    def submit(self, job: Job) -> Job:
        self.store.save(job)
        if self.audit:
            self.audit.append("job.submit", actor=job.actor, target=job.id,
                              details={"kind": job.kind, "steps": len(job.steps),
                                       "library": job.params.get("library"),
                                       "not_before": job.not_before})  # fmt: skip
        self.wake.set()
        return job

    def cancel(self, job_id: str) -> bool:
        job = self.store.load(job_id)
        if job.status in ("done", "failed", "cancelled"):
            return False
        job.status = "cancelled"
        job.finished = _now()
        self.store.save(job)
        self._cancelled.add(job_id)
        return True

    def _next(self) -> Job | None:
        now = _now()
        for job in self.store.all():  # oldest first; "running" = interrupted earlier
            if job.status in ("queued", "running") and (
                not job.not_before or job.not_before <= now
            ):
                return job
        return None

    async def run_forever(self) -> None:
        while True:
            job = await asyncio.to_thread(self._next)
            if job is None:
                self.wake.clear()
                with contextlib.suppress(TimeoutError):
                    await asyncio.wait_for(self.wake.wait(), 60)
                continue
            await self.run(job)

    async def run(self, job: Job) -> Job:
        resumed = job.status == "running"
        job.status, job.started = "running", job.started or _now()
        self.store.save(job)
        self.log(f"research job {job.id} ({job.kind}) {'resumed' if resumed else 'started'}")
        i = 0
        while i < len(job.steps):  # "prepare" adds steps as it finishes
            step = job.steps[i]
            i += 1
            if step.status != "pending":
                continue
            if job.id in self._cancelled or self.store.load(job.id).status == "cancelled":
                return self.store.load(job.id)
            started = time.monotonic()
            try:
                step.output = await self._step(job, step)
                step.status = "done"
            except Exception as exc:  # one failed paper shouldn't sink a night's work
                step.status, step.error = "failed", f"{type(exc).__name__}: {str(exc)[:300]}"
            step.seconds = round(time.monotonic() - started, 1)
            self.store.save(job)
        job.status = "done" if any(s.status == "done" for s in job.steps) else "failed"
        job.finished = _now()
        text = render(job)
        self.store.write_result(job, text)
        with contextlib.suppress(Exception):  # artifacts are a copy; the result file stays
            from cairnward.artifacts import ArtifactStore

            ArtifactStore(self.store.root.parent).write("job", job.id, "result.md", text,
                                                        f"job {job.status}")  # fmt: skip
        self.store.save(job)
        if self.audit:
            failed = sum(s.status == "failed" for s in job.steps)
            self.audit.append("job.finish", actor="cairnward", target=job.id,
                              outcome="ok" if job.status == "done" else "error",
                              details={"kind": job.kind, "steps": len(job.steps),
                                       "failed": failed})  # fmt: skip
        self.log(f"research job {job.id} {job.status}")
        return job

    async def _step(self, job: Job, step: Step) -> Any:
        library = job.params["library"]
        if step.name == "ask":
            if job.params.get("engine") == "paperqa":
                if self.deep is None:
                    raise RuntimeError("the PaperQA2 engine isn't available in this server")
                answer = await self.deep.ask(
                    library, step.params["question"], actor=f"job:{job.id}",
                    evidence_k=int(job.params.get("evidence_k", 10)),
                    skip_summary=bool(job.params.get("skip_summary", False)),
                )  # fmt: skip
            else:
                answer = await self.ask(library, step.params["question"], actor=f"job:{job.id}")
            return answer.as_dict()
        if step.name == "prepare":
            return await self._prepare(job, step)
        if step.name == "notes":
            research = self.research()
            try:
                rows = research.chunk_rows(library, [step.params["document_id"]])
            finally:
                research.close()
            chat = self.chat_for(library)
            budget = (chat.context_tokens or 8192) - 1500
            reply = await chat.ask([
                {"role": "system", "content": NOTES_SYSTEM},
                {"role": "user", "content": f"Review question: {job.params['question']}\n\n"
                                            f"Paper: {step.params['title']}\n\n"
                                            + _paper_text(rows, budget)},
            ], max_tokens=700)  # fmt: skip
            return {"title": step.params["title"], **parse_notes(reply.text)}
        if step.name == "table":
            from cairnward.research.cite import format_references, from_documents

            research = self.research()
            try:
                docs = {d.id: d for d in research.documents(library)}
                folder = research.get(library).folder
            finally:
                research.close()
            ids = [s.params["document_id"] for s in job.steps if s.name == "notes"
                   and s.status == "done"]  # fmt: skip
            items = from_documents([docs[i] for i in ids if i in docs], folder)
            style = job.params.get("style", "apa")
            entries, used = await asyncio.to_thread(format_references, items, style,
                                                    engine=self.cite_engine,
                                                    paths=self.paths)  # fmt: skip
            return {"bibliography": entries, "engine": used}
        raise ValueError(f"unknown step {step.name}")

    async def _prepare(self, job: Job, step: Step) -> dict[str, Any]:
        library, files = job.params["library"], step.params.get("files")
        indexed = await self.index(library, files, f"job:{job.id}") if self.index else None
        research = self.research()
        try:
            docs = research.documents(library)
        finally:
            research.close()
        by_path = {d.path: d for d in docs}
        wanted = files if files is not None else [d.path for d in docs]
        picked = [by_path[f] for f in wanted if f in by_path and by_path[f].status == "indexed"]
        skipped = [{"file": f, "why": by_path[f].status if f in by_path else "not found"}
                   for f in wanted if f not in {d.path for d in picked}]  # fmt: skip
        if not picked:
            raise RuntimeError("no indexed papers to review"
                               + (f" ({len(skipped)} skipped)" if skipped else ""))  # fmt: skip
        job.steps += [Step("notes", {"document_id": d.id, "title": d.title or Path(d.path).name})
                      for d in picked]  # fmt: skip
        job.steps.append(Step("table", {}))
        return {"papers": len(picked), "skipped": skipped, "indexed": indexed}


def _cell(text: str) -> str:
    return " ".join(str(text).split()).replace("|", "\\|")


def render(job: Job) -> str:
    lines = [f"# Research job {job.id} ({job.kind})", "",
             f"- library: {job.params.get('library')}", f"- created: {job.created}",
             f"- finished: {job.finished}", ""]  # fmt: skip
    if job.kind == "ask":
        for s in job.steps:
            lines.append(f"## {s.params['question']}")
            if s.status == "done":
                from cairnward.research.ask import Answer, Source

                a = dict(s.output)
                a["sources"] = [Source(**x) for x in a["sources"]]
                lines += ["", Answer(**a).render()]
            else:
                lines += ["", f"(failed: {s.error})", ""]
        return "\n".join(lines)
    lines += [f"Review question: {job.params.get('question')}", "", "## Comparison", ""]
    lines.append("| Paper | " + " | ".join(f.capitalize() for f in NOTE_FIELDS) + " |")
    lines.append("|" + "---|" * (len(NOTE_FIELDS) + 1))
    for s in job.steps:
        if s.name != "notes":
            continue
        if s.status == "done":
            n = s.output
            if n.get("raw"):
                lines.append(f"| {_cell(n['title'])} | " + _cell(n["raw"][:300]) + " |" + " |" * 4)
            else:
                lines.append(f"| {_cell(n['title'])} | "
                             + " | ".join(_cell(n[f]) for f in NOTE_FIELDS) + " |")  # fmt: skip
        else:
            lines.append(f"| {_cell(s.params['title'])} | (failed: {_cell(s.error or '')}) |"
                         + " |" * 4)  # fmt: skip
    prep = next((s for s in job.steps if s.name == "prepare" and s.status == "done"), None)
    if prep and prep.output.get("skipped"):
        lines += ["", "Not reviewed: " + "; ".join(f"{x['file']} ({x['why']})"
                                                 for x in prep.output["skipped"])]  # fmt: skip
    table = next((s for s in job.steps if s.name == "table" and s.status == "done"), None)
    if table:
        lines += ["", "## References", "", *[f"{e}\n" for e in table.output["bibliography"]]]
    lines += ["", "Notes were written by a model from each paper's text: check them against "
              "the papers (`cairnward library check`) before relying on them."]  # fmt: skip
    return "\n".join(lines)
