"""Citation-existence evaluation (FR-RES-5): does `cairnward library check` catch invented
quotations and references? Cases with known answers are in cases.json; see README.md.

Usage: uv run python evals/citation_existence/run_eval.py [--env litreview-offline]
Prints a JSON line per case and a summary; exits 0 when every case passes.
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

LDQ, RDQ = chr(0x201C), chr(0x201D)


def draft(cases: dict[str, Any]) -> str:
    """A draft holding every case: quotations in the body, references in their section."""
    lines = ["# Review", ""]
    for q in cases["quotes"]:
        left, right = (LDQ, RDQ) if q.get("curly") else ('"', '"')
        lines.append(f"One paper says {left}{q['text']}{right} about this.")
    lines += ["", "## References", ""]
    lines += [f"- {r['text']}" for r in cases["references"]]
    return "\n".join(lines) + "\n"


def score(cases: dict[str, Any], report: dict[str, Any]) -> list[dict[str, Any]]:
    """One row per case. `missed`: something invented passed, which must never happen."""
    rows: list[dict[str, Any]] = []
    quotes = {q["quote"]: q for q in report["quotes"]}
    for case in cases["quotes"]:
        got = quotes.get(case["text"])
        ok = bool(got and got["ok"])
        rows.append({"id": f"quote:{case['id']}", "pass": got is not None and ok == case["real"],
                     "missed": ok and not case["real"], "got": got and got["status"]})  # fmt: skip
    refs = {r["entry"]: r for r in report["references"]}
    for case in cases["references"]:
        found = refs.get(case["text"])
        status = found["status"] if found else None
        rows.append({"id": f"reference:{case['id']}", "pass": status == case["expect"],
                     "missed": status == "in_library" and case["expect"] != "in_library",
                     "got": status})  # fmt: skip
    return rows


def cw(*args: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run([sys.executable, "-m", "cairnward", *args], capture_output=True,
                          text=True, check=False)  # fmt: skip


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--env", default="litreview-offline")
    args = ap.parse_args()
    cases = json.loads((HERE / "cases.json").read_text("utf-8"))
    name = f"eval-citations-{int(time.time())}"
    with tempfile.TemporaryDirectory(prefix="cw-cite-") as tmp:
        folder = Path(tmp) / "papers"
        folder.mkdir()
        for file, paper in cases["papers"].items():
            pages = [[p] for p in paper["pages"]]
            (folder / file).write_bytes(make_pdf(pages, title=paper["title"]))
        draft_file = Path(tmp) / "draft.md"
        draft_file.write_text(draft(cases), encoding="utf-8")
        try:
            for step in (["library", "create", name, str(folder), "--env", args.env],
                         ["library", "index", name]):  # fmt: skip
                r = cw(*step)
                if r.returncode != 0:
                    print((r.stdout + r.stderr)[-800:])
                    return 2
            r = cw("library", "check", name, str(draft_file), "--json")
            if r.returncode not in (0, 1):  # 1: it found problems, as it should
                print((r.stdout + r.stderr)[-800:])
                return 2
            rows = score(cases, json.loads(r.stdout))
        finally:
            cw("library", "remove", name)
    for row in rows:
        print(json.dumps(row))
    passed = sum(row["pass"] for row in rows)
    missed = sum(row["missed"] for row in rows)
    print(json.dumps({"summary": {"cases": len(rows), "passed": passed, "missed": missed}}))
    return 0 if passed == len(rows) else 1


if __name__ == "__main__":
    raise SystemExit(main())
