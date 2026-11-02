"""Review-pipeline evaluation (FR-RES-4): screening, notes table and synthesis with your
real models, against a running `cairnward serve`.

Builds a small library of synthetic papers (three on sleep and working memory with known
methods and samples on known pages, one off-topic), runs `cairnward research review`,
waits for it, and scores:
- screening: the off-topic paper excluded, the others kept;
- notes: each included paper's method and sample found (keywords) with a page reference;
- synthesis: written, cites only papers that exist, cites at least two of them;
- references: a .bib entry per included paper.

On a CPU this takes a while (D63: ~7 minutes per paper read in full).
    uv run python evals/litreview_table/run_eval.py [--env litreview-offline] [--keep]
Prints one JSON line per check and a summary; exits 0 when everything passes.
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
from evals.pdf_qa.run_eval import FILLER, lines  # noqa: E402
from tests.fakes.pdf import make_pdf  # noqa: E402

PAPERS = {
    "walker.pdf": {
        "title": "Sleep duration and working memory in older adults",
        "pages": [
            "Abstract. We followed 40 older adults for one month with wrist actigraphy "
            "to see whether longer sleep goes with better working memory.",
            "Method. A cohort study: 40 adults aged 65 to 80 wore actigraphs; working "
            "memory was measured with a digit span task.",
            "Findings. Each extra hour of sleep went with a higher digit span score.",
        ],
        "expect": {"method": ["actigraph"], "sample": ["40"]},
    },
    "ito.pdf": {
        "title": "Napping and working memory in students",
        "pages": [
            "Abstract. A randomised trial of afternoon naps and working memory in "
            "university students.",
            "Method. A randomised controlled trial with 120 students assigned to a "
            "30-minute nap or rest; working memory measured with an n-back task.",
            "Findings. Napping improved n-back accuracy compared with rest.",
        ],
        "expect": {"method": ["random"], "sample": ["120"]},
    },
    "ruiz.pdf": {
        "title": "Sleep restriction and memory in shift workers",
        "pages": [
            "Abstract. A survey of sleep and memory complaints among night-shift nurses.",
            "Method. A cross-sectional survey of 310 nurses on night shifts.",
            "Findings. Shorter sleep went with more memory complaints.",
        ],
        "expect": {"method": ["survey"], "sample": ["310"]},
    },
    "glacier.pdf": {
        "title": "Glacier mass balance from drones",
        "pages": [
            "Abstract. We measure glacier surface change with drone photogrammetry.",
            "Method. Repeat drone flights over two Alpine glaciers.",
            "Findings. Both glaciers lost mass in every season.",
        ],
        "expect": None,  # off topic: screening should exclude it
    },
}
QUESTION = "Is sleep duration related to working memory in adults?"


def cw(*args: str, timeout: float | None = None) -> subprocess.CompletedProcess[str]:
    return subprocess.run([sys.executable, "-m", "cairnward", *args], capture_output=True,
                          text=True, check=False, timeout=timeout)  # fmt: skip


def build(folder: Path) -> None:
    for name, paper in PAPERS.items():
        pages = [lines(f"{FILLER * 6} {p} {FILLER * 6}") for p in paper["pages"]]
        (folder / name).write_bytes(make_pdf(pages, title=paper["title"]))


def job_file(job_id: str) -> Path:
    from cairnward.config import default_paths

    return default_paths().data_dir / "jobs" / job_id / "job.json"


def score(job: dict[str, Any], bib: str) -> list[dict[str, Any]]:
    steps = job["steps"]
    checks: list[dict[str, Any]] = []
    screened = {s["output"]["title"]: s["output"]["decision"] for s in steps
                if s["name"] == "screen" and s["status"] == "done"}  # fmt: skip
    for name, paper in PAPERS.items():
        decision = screened.get(paper["title"])
        want = "exclude" if paper["expect"] is None else "keep"
        ok = decision == "exclude" if want == "exclude" else decision in ("include", "unsure")
        checks.append({"check": f"screen {name}", "pass": ok, "got": decision})
    notes = {s["output"]["title"]: s["output"] for s in steps
             if s["name"] == "notes" and s["status"] == "done"}  # fmt: skip
    for name, paper in PAPERS.items():
        if paper["expect"] is None:
            continue
        n = notes.get(paper["title"], {})
        for field, words in paper["expect"].items():
            text = str(n.get(field, n.get("raw", ""))).lower()
            ok = all(w in text for w in words) and "p." in text
            checks.append({"check": f"notes {name} {field}", "pass": ok, "got": text[:160]})
    synth = next((s for s in steps if s["name"] == "synthesis"), None)
    out = (synth or {}).get("output") or {}
    cited = len(out.get("papers", [])) - len(out.get("uncited", []))
    checks.append({"check": "synthesis written, valid citations",
                   "pass": bool(out.get("text")) and not out.get("invalid_citations")
                   and cited >= 2, "got": {"invalid": out.get("invalid_citations"),
                                           "cited": cited}})  # fmt: skip
    entries = bib.count("\n@") + (1 if bib.startswith("@") else 0)
    checks.append({"check": ".bib entries", "pass": entries == len(notes), "got": entries})
    return checks


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--env", default="litreview-offline")
    ap.add_argument("--keep", action="store_true", help="keep the eval library afterwards")
    ap.add_argument("--timeout-min", type=int, default=180)
    args = ap.parse_args()
    name = f"eval-review-{int(time.time())}"
    with tempfile.TemporaryDirectory(prefix="cw-review-") as tmp:
        build(Path(tmp))
        try:
            for step in (["library", "create", name, tmp, "--env", args.env],
                         ["library", "index", name]):  # fmt: skip
                r = cw(*step)
                if r.returncode != 0:
                    print(r.stdout + r.stderr)
                    return 2
            r = cw(
                "research",
                "review",
                name,
                "--question",
                QUESTION,
                "--criteria",
                "studies of adults that measure sleep and working memory",
            )
            if r.returncode != 0:
                print(r.stdout + r.stderr)
                return 2
            job_id = r.stdout.split()[1]
            deadline = time.monotonic() + args.timeout_min * 60
            while time.monotonic() < deadline:
                shown = cw("jobs", "show", job_id).stdout
                if " done " in shown or " failed " in shown:
                    break
                time.sleep(20)
            job = json.loads(job_file(job_id).read_text(encoding="utf-8"))
            bib_path = job_file(job_id).with_name("references.bib")
            bib = bib_path.read_text(encoding="utf-8") if bib_path.exists() else ""
        finally:
            if not args.keep:
                cw("library", "remove", name)
    checks = score(job, bib)
    for c in checks:
        print(json.dumps(c))
    seconds = sum(s.get("seconds", 0) for s in job["steps"])
    passed = sum(c["pass"] for c in checks)
    print(json.dumps({"summary": {"checks": len(checks), "passed": passed,
                                  "job_seconds": round(seconds)}}))  # fmt: skip
    return 0 if passed == len(checks) else 1


if __name__ == "__main__":
    raise SystemExit(main())
