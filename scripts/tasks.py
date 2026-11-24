"""The project's task list, kept with Backlog.md in backlog/ (ADR-0022).

    uv run python scripts/tasks.py next      # open work, most urgent first
    uv run python scripts/tasks.py render    # rewrite the generated parts of docs/status.md
    uv run python scripts/tasks.py check     # what CI runs: the rules below, and status.md current

Backlog.md checks a task's fields when it's changed through its CLI or MCP tools, but not when a
file is edited by hand. `check` runs Backlog.md's own `doctor` (duplicate IDs, dependency loops),
then Cairnward's rules on every task:
- type, status, priority and labels are ones configured in backlog/config.yml;
- an `action` is the owner's (assignee @owner), `work` is an agent's (@agent);
- open actions and work have a priority, unless they wait on another task;
- a `check` has exactly one label, its group;
- dependencies name tasks that exist.
Then it checks that docs/status.md's generated sections match the list.

The Backlog.md CLI is pinned in tools/backlog: `cd tools/backlog && npm ci --ignore-scripts`.
"""

from __future__ import annotations

import argparse
import json
import os
import re
import shutil
import subprocess
import sys
from collections.abc import Callable
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parent.parent
STATUS = ROOT / "docs" / "status.md"
TOOL = ROOT / "tools" / "backlog" / "node_modules" / ".bin"
BLOCKS = ("owner", "agent", "checks")
NOTE = (
    "<!-- Generated from backlog/ by scripts/tasks.py; change tasks with `backlog`, not here. -->"
)
OWNER, AGENT = "@owner", "@agent"
PRIORITY_ORDER = {"high": 0, "medium": 1, "low": 2}


class TaskError(ValueError):
    """The task list, its tool or the status page is not as it should be."""


@dataclass(frozen=True)
class Task:
    id: str
    title: str
    status: str
    type: str | None
    priority: str | None
    assignees: tuple[str, ...]
    labels: tuple[str, ...]
    dependencies: tuple[str, ...]
    updated: str | None
    description: str = ""

    @property
    def number(self) -> int:
        return int(self.id.split("-")[-1])

    @property
    def done(self) -> bool:
        return self.status == "Done"


@dataclass(frozen=True)
class Settings:
    statuses: tuple[str, ...]
    types: tuple[str, ...]
    priorities: tuple[str, ...]
    labels: tuple[str, ...]  # the check groups, in the order status.md shows them
    final: str = field(default="Done")


# ------------------------------------------------------------------ the Backlog.md CLI


def backlog_bin() -> str:
    """The pinned CLI in tools/backlog, or CAIRNWARD_BACKLOG, or `backlog` on the PATH."""
    if env := os.environ.get("CAIRNWARD_BACKLOG"):
        return env
    pinned = shutil.which("backlog", path=str(TOOL))
    found = pinned or shutil.which("backlog")
    if found is None:
        raise TaskError(
            "the Backlog.md CLI isn't installed: cd tools/backlog && npm ci --ignore-scripts"
        )
    return found


Runner = Callable[[list[str]], str]


def cli_in(cwd: Path) -> Runner:
    """Run the Backlog.md CLI in a folder; its output, or TaskError with what it said."""

    def run(args: list[str]) -> str:
        done = subprocess.run(  # noqa: S603 - the pinned Backlog.md CLI, fixed arguments
            [backlog_bin(), *args], cwd=cwd, capture_output=True, text=True, encoding="utf-8"
        )
        if done.returncode != 0:
            said = f"{done.stdout}{done.stderr}".strip()
            raise TaskError(f"backlog {' '.join(args)} failed:\n{said}")
        return done.stdout

    return run


run_cli = cli_in(ROOT)


def settings(run: Runner = run_cli) -> Settings:
    def get(key: str) -> tuple[str, ...]:
        return tuple(v.strip() for v in run(["config", "get", key]).strip().split(",") if v.strip())

    return Settings(get("statuses"), get("types"), get("priorities"), get("labels"))


def from_json(raw: dict[str, Any], description: str | None = None) -> Task:
    """A task from Backlog.md's versioned JSON (`task list --json` / `task view --json`)."""
    return Task(
        id=raw["id"],
        title=raw["title"],
        status=raw["status"],
        type=raw.get("type"),
        priority=raw.get("priority"),
        assignees=tuple(raw.get("assignees") or ()),
        labels=tuple(raw.get("labels") or ()),
        dependencies=tuple(raw.get("dependencies") or ()),
        updated=raw.get("updatedAt"),
        description=(description if description is not None else raw.get("description")) or "",
    )


def load(run: Runner = run_cli) -> list[Task]:
    listed = json.loads(run(["task", "list", "--json"]))
    if listed.get("schemaVersion") != 1:
        raise TaskError(f"unexpected Backlog.md JSON schema {listed.get('schemaVersion')!r}")

    def described(raw: dict[str, Any]) -> Task:
        view = json.loads(run(["task", "view", raw["id"], "--json"]))["task"]
        return from_json(raw, view.get("description"))

    with ThreadPoolExecutor(max_workers=8) as pool:
        return list(pool.map(described, listed["tasks"]))


# ------------------------------------------------------------------ Cairnward's rules


def problems(tasks: list[Task], cfg: Settings) -> list[str]:
    out: list[str] = []
    ids = {t.id for t in tasks}
    priorities = {p.lower() for p in cfg.priorities}
    for t in tasks:

        def bad(msg: str, t: Task = t) -> None:
            out.append(f"{t.id} ({t.title[:40]}): {msg}")

        if t.status not in cfg.statuses:
            bad(f"status '{t.status}' isn't one of {', '.join(cfg.statuses)}")
        if t.type not in cfg.types:
            bad(f"type must be one of {', '.join(cfg.types)}, not {t.type!r}")
        if t.priority is not None and t.priority.lower() not in priorities:
            bad(f"priority '{t.priority}' isn't one of {', '.join(cfg.priorities)}")
        for label in t.labels:
            if label not in cfg.labels:
                bad(f"label '{label}' isn't configured in backlog/config.yml")
        if t.type == "action" and t.assignees != (OWNER,):
            bad(f"an action is the owner's: assignee {OWNER}")
        if t.type == "work" and t.assignees != (AGENT,):
            bad(f"work is an agent's: assignee {AGENT}")
        open_work = t.type in ("action", "work") and not t.done
        if open_work and t.priority is None and not waiting_on(t, tasks):
            bad("open actions and work need a priority, unless they wait on another task")
        if t.type == "check" and len(t.labels) != 1:
            bad(f"a check has exactly one label, its group ({', '.join(cfg.labels)})")
        for dep in t.dependencies:
            if dep not in ids:
                bad(f"depends on {dep}, which isn't in the list")
    return out


def doctor(run: Runner = run_cli) -> None:
    """Backlog.md's own checks: duplicate IDs, self-references and dependency loops."""
    run(["doctor"])


# ------------------------------------------------------------------ rendering


def for_docs(text: str) -> str:
    """Task text links from the repository root (/docs/x.md); status.md is in docs/."""
    text = re.sub(r"\]\(/docs/", "](", text)
    return re.sub(r"\]\(/(?!/)", "](../", text)


def one_line(text: str) -> str:
    return " ".join(for_docs(text).split()).replace("|", "\\|")


def _urgency(t: Task) -> tuple[int, int]:
    return (PRIORITY_ORDER.get((t.priority or "").lower(), 9), t.number)


def waiting_on(t: Task, tasks: list[Task]) -> list[str]:
    done = {x.id for x in tasks if x.done}
    return [d for d in t.dependencies if d not in done]


def _sentence(t: Task) -> str:
    title, detail = one_line(t.title), one_line(t.description)
    return f"{title}. {detail}" if detail else title


def render_owner(tasks: list[Task]) -> str:
    mine = [t for t in tasks if t.type == "action"]
    open_ = sorted((t for t in mine if not t.done), key=_urgency)
    done = sorted(
        (t for t in mine if t.done), key=lambda t: (t.updated or "", t.number), reverse=True
    )
    lines = ["| # | Priority | What | Why it matters |", "|---|---|---|---|"]
    for t in open_:
        what = one_line(t.title)
        if wait := waiting_on(t, tasks):
            what += f" (waiting on {', '.join(wait)})"
        lines.append(
            f"| {t.id} | {(t.priority or '').capitalize()} | {what} | {one_line(t.description)} |"
        )
    if done:
        lines += ["", "Done:"]
        lines += [f"- **{t.id}** ({(t.updated or 'undated')[:10]}): {_sentence(t)}" for t in done]
    return "\n".join(lines)


def render_agent(tasks: list[Task]) -> str:
    todo = [t for t in tasks if t.type == "work" and not t.done]
    todo.sort(key=lambda t: (bool(waiting_on(t, tasks)), *_urgency(t)))
    lines = []
    for t in todo:
        wait = waiting_on(t, tasks)
        when = f"waiting on {', '.join(wait)}" if wait else (t.priority or "").capitalize()
        lines.append(f"- **{t.id}** ({when}): {_sentence(t)}")
    return "\n".join(lines) if lines else "Nothing open."


GROUP_NAMES = {
    "could-reveal-bugs": "Could reveal real bugs",
    "never-run-for-real": "Never run for real",
    "project-level": "Project-level",
    "stand-in-only": "Features tested only against a stand-in",
}


def render_checks(tasks: list[Task], cfg: Settings) -> str:
    out: list[str] = []
    for label in cfg.labels:
        items = [t for t in tasks if t.type == "check" and label in t.labels]
        if not items:
            continue
        out += ["", f"### {GROUP_NAMES.get(label, label)}"]
        for t in sorted(items, key=lambda t: (t.done, t.number)):
            box = "x" if t.done else " "
            date = f", {t.updated[:10]}" if t.done and t.updated else ""
            rest = f": {one_line(t.description)}" if t.description else ""
            out.append(f"- [{box}] {one_line(t.title)}{date}{rest} ({t.id})")
    return "\n".join(out).lstrip("\n")


def _block(name: str) -> re.Pattern[str]:
    return re.compile(rf"(<!-- tasks:{name} -->\n)(?:.*?\n)?(<!-- /tasks:{name} -->)", re.DOTALL)


def render_status(text: str, tasks: list[Task], cfg: Settings) -> str:
    bodies = {
        "owner": render_owner(tasks),
        "agent": render_agent(tasks),
        "checks": render_checks(tasks, cfg),
    }
    for name in BLOCKS:
        pattern = _block(name)
        if not pattern.search(text):
            raise TaskError(f"status.md has no <!-- tasks:{name} --> block")
        body = f"{NOTE}\n{bodies[name]}\n"

        def fill(m: re.Match[str], body: str = body) -> str:
            return m.group(1) + body + m.group(2)

        text = pattern.sub(fill, text, count=1)
    return text


def next_lines(tasks: list[Task], kind: str | None, limit: int) -> list[str]:
    todo = [t for t in tasks if not t.done and (kind is None or t.type == kind)]
    todo.sort(key=lambda t: (bool(waiting_on(t, tasks)), *_urgency(t)))
    lines = []
    for t in todo[:limit]:
        wait = waiting_on(t, tasks)
        tag = f"waiting on {','.join(wait)}" if wait else (t.priority or "-").capitalize()
        lines.append(f"{t.id:<8} {t.type or '':<6} {tag:<10} {t.title}")
    return lines


# ------------------------------------------------------------------ command line


def main(argv: list[str] | None = None, run: Runner = run_cli) -> int:
    ap = argparse.ArgumentParser(description=(__doc__ or "").splitlines()[0])
    sub = ap.add_subparsers(dest="cmd", required=True)
    nx = sub.add_parser("next", help="open work, most urgent first")
    nx.add_argument("--kind", choices=["action", "work", "check"])
    nx.add_argument("--limit", type=int, default=15)
    sub.add_parser("render", help="rewrite the generated blocks in docs/status.md")
    sub.add_parser("check", help="Backlog.md's doctor, Cairnward's rules, status.md current")
    args = ap.parse_args(argv)
    try:
        tasks = load(run)
        if args.cmd == "next":
            print("\n".join(next_lines(tasks, args.kind, args.limit)) or "Nothing open.")
            return 0
        cfg = settings(run)
        found = problems(tasks, cfg)
        current = STATUS.read_text(encoding="utf-8")
        wanted = render_status(current, tasks, cfg)
        if args.cmd == "render":
            if found:
                print("Not rendered; fix these first:\n" + "\n".join(found), file=sys.stderr)
                return 1
            if wanted != current:
                STATUS.write_text(wanted, encoding="utf-8")
                print("docs/status.md updated")
            return 0
        doctor(run)
        if found:
            print("\n".join(found), file=sys.stderr)
            return 1
        if wanted != current:
            print("docs/status.md is behind the task list: run "
                  "`uv run python scripts/tasks.py render`", file=sys.stderr)  # fmt: skip
            return 1
        print(f"{len(tasks)} tasks follow the rules; docs/status.md is current")
        return 0
    except TaskError as e:
        print(e, file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(main())
