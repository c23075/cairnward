"""lit.ask evaluation (FR-RES-2, M1 exit criterion): page-cited answers from PDFs.

Builds a small library of synthetic papers whose facts sit on known pages, asks the
questions in questions.json through Cairnward (the running server when there is one,
so it uses your configured models), and scores each answer:
- cited: at least one source, and the expected file AND page among them;
- content: the expected keywords appear in the answer;
- honest: unanswerable questions get "I can't answer this from these sources";
- invented: citations to passages that weren't given, and quotations not found in the
  papers (must be zero; this is also the citation-existence check, FR-RES-5).

Usage (after `cairnward serve`, with the litreview-offline models pulled):
    uv run python evals/pdf_qa/run_eval.py [--env litreview-offline] [--keep]
Prints a table and a JSON summary; exits 0 when every check passes.
"""

from __future__ import annotations

import argparse
import json
import subprocess
import sys
import tempfile
import time
from pathlib import Path
from typing import Any

HERE = Path(__file__).parent
sys.path.insert(0, str(HERE.parent.parent))
from tests.fakes.pdf import make_pdf  # noqa: E402  (the repo's tiny PDF writer)


def lines(page: str, width: int = 80) -> list[str]:
    out, cur = [], ""
    for word in page.split():
        if len(cur) + len(word) + 1 > width:
            out.append(cur)
            cur = word
        else:
            cur = f"{cur} {word}".strip()
    return [*out, cur] if cur else out


FILLER = (
    "This page continues the study's report. The surrounding text describes the general "
    "setting and does not add new findings. "
)


def build(folder: Path, spec: dict[str, Any]) -> None:
    """Each fact sits in the middle of a page padded with neutral text to well over two
    chunks (~350 tokens each), so some chunk holding the fact lies wholly on its page and
    page citations can be checked."""
    for name, paper in spec["papers"].items():
        pages = [lines(f"{FILLER * 14} {p} {FILLER * 14}") for p in paper["pages"]]
        (folder / name).write_bytes(make_pdf(pages, title=paper["title"]))


def cw(*args: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run([sys.executable, "-m", "cairnward", *args], capture_output=True,
                          text=True, check=False)  # fmt: skip


def score(q: dict[str, Any], a: dict[str, Any]) -> dict[str, Any]:
    exp = q["expect"]
    res: dict[str, Any] = {
        "id": q["id"],
        # invented citations and quotations not found in the papers (FR-RES-5)
        "invented": len(a.get("invalid_citations", [])) + len(a.get("quote_problems", [])),
        "seconds": round(a.get("seconds", 0), 1),
    }
    if exp.get("unanswerable"):
        res["honest"] = bool(a.get("cannot_answer"))
        res["pass"] = res["honest"] and not res["invented"]
        return res
    pages_ok = False
    for s in a.get("sources", []):
        lo, _, hi = s["pages"].partition("-")
        if s["file"] == exp["file"] and int(lo) <= exp["page"] <= int(hi or lo):
            pages_ok = True
    res["cited"] = pages_ok
    res["content"] = all(k.lower() in a.get("text", "").lower() for k in exp["keywords"])
    res["pass"] = res["cited"] and res["content"] and not res["invented"]
    return res


class EvalError(Exception):
    pass


def run(env: str = "litreview-offline", engines: list[str] | None = None, *, keep: bool = False,
        emit: Any = print) -> dict[str, Any]:  # fmt: skip
    """Build the library, ask every question with each engine, score; returns
    {"summaries": {engine: {...}}, "results": [...]}. Needs `cairnward serve`."""
    engines = engines or ["lean"]
    spec = json.loads((HERE / "questions.json").read_text())
    name = f"eval-pdf-qa-{int(time.time())}"
    results: list[dict[str, Any]] = []
    summaries: dict[str, Any] = {}
    with tempfile.TemporaryDirectory(prefix="cw-pdfqa-") as tmp:
        folder = Path(tmp)
        build(folder, spec)
        try:
            for step in (["library", "create", name, str(folder), "--env", env],
                         ["library", "index", name]):  # fmt: skip
                r = cw(*step)
                if r.returncode != 0:
                    raise EvalError(f"`cairnward {' '.join(step[:2])}` failed: "
                                    f"{(r.stdout + r.stderr).strip()[-500:]}")  # fmt: skip
            for engine in engines:
                mine = []
                for q in spec["questions"]:
                    t0 = time.monotonic()
                    r = cw("library", "ask", name, q["question"], "--json", "--engine", engine)
                    took = round(time.monotonic() - t0, 1)
                    if r.returncode != 0:
                        raise EvalError(f"ask failed ({engine}): {(r.stdout + r.stderr)[-500:]}")
                    answer = json.loads(r.stdout)
                    answer["seconds"] = answer.get("seconds") or took  # wall clock if not given
                    res = score(q, answer) | {"engine": engine}
                    mine.append(res)
                    emit(json.dumps(res))
                results += mine
                summaries[engine] = {
                    "questions": len(mine), "passed": sum(r["pass"] for r in mine),
                    "invented_citations": sum(r["invented"] for r in mine),
                    "seconds_total": round(sum(r["seconds"] for r in mine), 1),
                }  # fmt: skip
        finally:
            if not keep:
                cw("library", "remove", name)
    return {"summaries": summaries, "results": results}


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--env", default="litreview-offline")
    ap.add_argument("--keep", action="store_true", help="keep the eval library afterwards")
    ap.add_argument("--engine", choices=["lean", "paperqa"], default="lean")
    ap.add_argument("--compare", action="store_true",
                    help="run both engines on the same questions (FR-RES-13)")  # fmt: skip
    args = ap.parse_args()
    engines = ["lean", "paperqa"] if args.compare else [args.engine]
    try:
        out = run(args.env, engines, keep=args.keep)
    except EvalError as exc:
        print(exc)
        return 2
    for engine, summary in out["summaries"].items():
        print(json.dumps({"engine": engine, "summary": summary}))
    return 0 if all(s["passed"] == s["questions"] for s in out["summaries"].values()) else 1


if __name__ == "__main__":
    raise SystemExit(main())
