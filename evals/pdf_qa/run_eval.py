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


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--env", default="litreview-offline")
    ap.add_argument("--keep", action="store_true", help="keep the eval library afterwards")
    args = ap.parse_args()
    spec = json.loads((HERE / "questions.json").read_text())
    name = f"eval-pdf-qa-{int(time.time())}"
    with tempfile.TemporaryDirectory(prefix="cw-pdfqa-") as tmp:
        folder = Path(tmp)
        build(folder, spec)
        for step in (["library", "create", name, str(folder), "--env", args.env],
                     ["library", "index", name]):  # fmt: skip
            r = cw(*step)
            if r.returncode != 0:
                print(r.stdout + r.stderr)
                return 2
        results = []
        try:
            for q in spec["questions"]:
                r = cw("library", "ask", name, q["question"], "--json")
                if r.returncode != 0:
                    print(r.stdout + r.stderr)
                    return 2
                res = score(q, json.loads(r.stdout))
                results.append(res)
                print(json.dumps(res))
        finally:
            if not args.keep:
                cw("library", "remove", name)
    summary = {
        "questions": len(results), "passed": sum(r["pass"] for r in results),
        "invented_citations": sum(r["invented"] for r in results),
        "seconds_total": round(sum(r["seconds"] for r in results), 1),
    }  # fmt: skip
    print(json.dumps({"summary": summary}))
    return 0 if summary["passed"] == summary["questions"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
