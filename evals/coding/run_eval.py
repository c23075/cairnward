#!/usr/bin/env python3
"""Coding evaluation: can an agent, run through Cairnward, fix a failing test?

Copies a task's repository to a fresh folder, runs the agent with `cairnward run`, then
checks the result (the task's test command) and reads the run's model calls, tokens and
time from Cairnward's audit log. Needs `cairnward serve` running.

    python evals/coding/run_eval.py --profile aider
    python evals/coding/run_eval.py --profile qwen-code --runs 3 --model cw/coding:high

Agent commands are defaults for non-interactive use; adjust with --agent-cmd (use {prompt}
and {files} placeholders).
"""

from __future__ import annotations

import argparse
import json
import shlex
import shutil
import subprocess
import sys
import tempfile
import time
from pathlib import Path

HERE = Path(__file__).resolve().parent
AGENT_COMMANDS = {
    "aider": "aider --yes-always --no-auto-commits --no-show-model-warnings --no-check-update "
             "--message {prompt} {files}",
    "qwen-code": "qwen --yolo -p {prompt}",
    "opencode": "opencode run {prompt}",
    "goose": "goose run --no-session -t {prompt}",
}  # fmt: skip


def cairnward(*args: str, check: bool = True) -> subprocess.CompletedProcess[str]:
    return subprocess.run([sys.executable, "-m", "cairnward", *args], capture_output=True,
                          text=True, check=check)  # fmt: skip


def run_records(run_id: str) -> list[dict]:
    out = cairnward("audit", "log", "--last", "100000", "--json").stdout
    return [r for r in map(json.loads, out.splitlines()) if r.get("run_id") == run_id]


def one(task: dict, profile: str, agent_cmd: str, model: str | None, env: str) -> dict:
    with tempfile.TemporaryDirectory(prefix="cw-eval-") as tmp:
        ws = Path(tmp) / "repo"
        shutil.copytree(HERE / task["name"] / "repo", ws)
        subprocess.run(["git", "init", "-q"], cwd=ws, check=True)
        subprocess.run(["git", "add", "."], cwd=ws, check=True)
        subprocess.run(["git", "-c", "user.email=eval@cw", "-c", "user.name=eval", "commit", "-qm",
                        "task"], cwd=ws, check=True)  # fmt: skip
        cmd = agent_cmd.format(prompt=shlex.quote(task["prompt"]),
                               files=" ".join(shlex.quote(f) for f in task["files"]))  # fmt: skip
        args = ["run", "--profile", profile, "--env", env, "--workspace", str(ws)]
        if model:
            args += ["--model", model]
        started = time.monotonic()
        res = subprocess.run([sys.executable, "-m", "cairnward", *args, "--", *shlex.split(cmd)],
                             cwd=ws, stdin=subprocess.DEVNULL, capture_output=True, text=True,
                             check=False)  # fmt: skip
        elapsed = time.monotonic() - started
        check = subprocess.run(task["check"], cwd=ws, capture_output=True, text=True, check=False)
        tests = [f for f in task["files"] if f.startswith("test_")]
        tests_untouched = subprocess.run(["git", "diff", "--quiet", "--", *tests],
                                         cwd=ws, check=False).returncode == 0  # fmt: skip
    last_start = cairnward("audit", "log", "--action", "run.start", "--last", "1", "--json").stdout
    run_id = json.loads(last_start)["run_id"] if last_start.strip() else ""
    records = run_records(run_id) if run_id else []
    posts = [r for r in records if r["action"] == "model.call" and r["phase"] == "post"]
    return {
        "profile": profile,
        "passed": check.returncode == 0 and tests_untouched,
        "tests_untouched": tests_untouched,
        "agent_exit": res.returncode,
        "seconds": round(elapsed, 1),
        "model_calls": len(posts),
        "prompt_tokens": sum(p["details"].get("prompt_tokens") or 0 for p in posts),
        "completion_tokens": sum(p["details"].get("completion_tokens") or 0 for p in posts),
        "run_id": run_id[:8],
    }


def main() -> int:
    ap = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    ap.add_argument("--profile", default="aider")
    ap.add_argument("--task", default="fix_failing_test")
    ap.add_argument("--runs", type=int, default=1)
    ap.add_argument("--model", help="virtual model, e.g. cw/coding:high")
    ap.add_argument("--env", default="coding")
    ap.add_argument("--agent-cmd", help="override the agent command ({prompt}, {files})")
    ap.add_argument("--out", type=Path, help="write results as JSON")
    args = ap.parse_args()
    task = json.loads((HERE / args.task / "task.json").read_text())
    agent_cmd = args.agent_cmd or AGENT_COMMANDS.get(args.profile)
    if not agent_cmd:
        print(f"no default command for profile {args.profile}; pass --agent-cmd", file=sys.stderr)
        return 2
    if cairnward("status", check=False).returncode != 0:
        print("Cairnward isn't running: start `cairnward serve` first", file=sys.stderr)
        return 2
    results = []
    for i in range(args.runs):
        r = one(task, args.profile, agent_cmd, args.model, args.env)
        results.append(r)
        print(
            f"run {i + 1}: {'PASS' if r['passed'] else 'FAIL'}  {r['seconds']}s  "
            f"{r['model_calls']} calls  {r['prompt_tokens']} prompt + {r['completion_tokens']} "
            f"completion tokens  (run {r['run_id']})"
        )
    passed = sum(r["passed"] for r in results)
    print(f"\n{args.profile} on {args.task}: {passed}/{len(results)} passed")
    if args.out:
        args.out.write_text(json.dumps(results, indent=2))
    return 0 if passed == len(results) else 1


if __name__ == "__main__":
    raise SystemExit(main())
