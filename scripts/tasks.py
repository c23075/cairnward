"""The project's task list: docs/plan/tasks.json (ADR-0022).

    uv run python scripts/tasks.py next      # open work, most urgent first
    uv run python scripts/tasks.py render    # rewrite the generated parts of docs/status.md
    uv run python scripts/tasks.py check     # fail if the list is invalid or status.md is stale

The list is the only place pending work is written down. docs/status.md shows parts of it
between `<!-- tasks:NAME -->` and `<!-- /tasks:NAME -->` markers; never edit those by hand.
"""

from __future__ import annotations

import argparse
import datetime as dt
import json
import re
import sys
from dataclasses import dataclass, field
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
TASKS = ROOT / "docs" / "plan" / "tasks.json"
STATUS = ROOT / "docs" / "status.md"

KINDS = {"owner": "A", "agent": "W", "check": "C"}
STATES = {"open", "blocked", "done", "dropped"}
BLOCKS = ("owner", "agent", "checks")
NOTE = "<!-- Generated from plan/tasks.json by scripts/tasks.py; edit that file, not this. -->"


class TaskError(ValueError):
    """The task list or the status page is not as it should be."""


@dataclass(frozen=True)
class Task:
    id: str
    kind: str
    state: str
    title: str
    detail: str = ""
    priority: int | None = None
    done: str | None = None
    group: str | None = None
    blocked_by: tuple[str, ...] = field(default=())

    @property
    def number(self) -> int:
        return int(self.id[1:])


def parse(data: object) -> list[Task]:
    """Read and validate the list. Raises TaskError naming every problem found."""
    if not isinstance(data, dict) or not isinstance(data.get("items"), list):
        raise TaskError("tasks.json must be an object with an `items` list")
    problems: list[str] = []
    tasks: list[Task] = []
    for raw in data["items"]:
        if not isinstance(raw, dict):
            problems.append(f"not an object: {raw!r}")
            continue
        tid = str(raw.get("id", ""))
        kind = str(raw.get("kind", ""))
        state = str(raw.get("state", ""))
        title = str(raw.get("title", "")).strip()
        prio = raw.get("priority")
        done = raw.get("done")
        group = raw.get("group")
        blocked = raw.get("blocked_by", [])
        where = tid or repr(title[:30])
        if kind not in KINDS:
            problems.append(f"{where}: kind must be one of {sorted(KINDS)}")
        elif not re.fullmatch(KINDS[kind] + r"[1-9][0-9]*", tid):
            problems.append(f"{where}: a {kind} item's id is {KINDS[kind]} and a number")
        if state not in STATES:
            problems.append(f"{where}: state must be one of {sorted(STATES)}")
        if not title:
            problems.append(f"{where}: no title")
        if kind in ("owner", "agent") and state == "open" and prio not in (1, 2, 3):
            problems.append(f"{where}: an open {kind} item needs priority 1, 2 or 3")
        if prio is not None and prio not in (1, 2, 3):
            problems.append(f"{where}: priority must be 1, 2 or 3")
        if done is not None:
            try:
                dt.date.fromisoformat(str(done))
            except ValueError:
                problems.append(f"{where}: done must be a date, YYYY-MM-DD")
            if state != "done":
                problems.append(f"{where}: has a done date but its state is {state}")
        if kind == "check" and not group:
            problems.append(f"{where}: a check needs a group")
        if state == "blocked" and not blocked:
            problems.append(f"{where}: a blocked item names what blocks it in blocked_by")
        tasks.append(
            Task(
                id=tid,
                kind=kind,
                state=state,
                title=title,
                detail=str(raw.get("detail", "")).strip(),
                priority=prio if isinstance(prio, int) else None,
                done=str(done) if done is not None else None,
                group=str(group) if group else None,
                blocked_by=tuple(str(b) for b in blocked),
            )
        )
    seen: set[str] = set()
    for t in tasks:
        if t.id in seen:
            problems.append(f"{t.id}: used twice")
        seen.add(t.id)
    for t in tasks:
        for b in t.blocked_by:
            if b not in seen:
                problems.append(f"{t.id}: blocked_by names {b}, which isn't in the list")
    if problems:
        raise TaskError("\n".join(problems))
    return tasks


def load(path: Path = TASKS) -> list[Task]:
    return parse(json.loads(path.read_text(encoding="utf-8")))


def canonical(text: str) -> str:
    """The list in one fixed layout, so edits by different agents give small diffs."""
    return json.dumps(json.loads(text), indent=2, ensure_ascii=False) + "\n"


def _urgency(t: Task) -> tuple[int, int, int]:
    return (1 if t.state == "blocked" else 0, t.priority or 9, t.number)


def _sentence(t: Task) -> str:
    return f"{t.title}. {t.detail}" if t.detail else t.title


def render_owner(tasks: list[Task]) -> str:
    open_ = sorted((t for t in tasks if t.kind == "owner" and t.state == "open"), key=_urgency)
    done = sorted(
        (t for t in tasks if t.kind == "owner" and t.state == "done"),
        key=lambda t: (t.done or "", t.number),
        reverse=True,
    )
    lines = ["| # | Priority | What | Why it matters |", "|---|---|---|---|"]
    lines += [f"| {t.id} | {t.priority} | {t.title} | {t.detail} |" for t in open_]
    if done:
        lines += ["", "Done:"]
        lines += [f"- **{t.id}** ({t.done or 'undated'}): {_sentence(t)}" for t in done]
    return "\n".join(lines)


def render_agent(tasks: list[Task]) -> str:
    todo = sorted(
        (t for t in tasks if t.kind == "agent" and t.state in ("open", "blocked")), key=_urgency
    )
    lines = []
    for t in todo:
        when = (
            f"waiting on {', '.join(t.blocked_by)}"
            if t.state == "blocked"
            else f"priority {t.priority}"
        )
        lines.append(f"- **{t.id}** ({when}): {_sentence(t)}")
    return "\n".join(lines) if lines else "Nothing open."


def render_checks(tasks: list[Task]) -> str:
    groups: dict[str, list[Task]] = {}
    for t in tasks:
        if t.kind == "check" and t.state != "dropped":
            groups.setdefault(t.group or "", []).append(t)
    out: list[str] = []
    for name, items in groups.items():
        out += ["", f"### {name}"]
        for t in sorted(items, key=lambda t: (t.state == "done", t.number)):
            box = "x" if t.state == "done" else " "
            date = f", {t.done}" if t.done else ""
            rest = f": {t.detail}" if t.detail else ""
            out.append(f"- [{box}] {t.title}{date}{rest} ({t.id})")
    return "\n".join(out).lstrip("\n")


RENDERERS = {"owner": render_owner, "agent": render_agent, "checks": render_checks}


def _block(name: str) -> re.Pattern[str]:
    return re.compile(rf"(<!-- tasks:{name} -->\n)(?:.*?\n)?(<!-- /tasks:{name} -->)", re.DOTALL)


def render_status(text: str, tasks: list[Task]) -> str:
    for name in BLOCKS:
        pattern = _block(name)
        if not pattern.search(text):
            raise TaskError(f"status.md has no <!-- tasks:{name} --> block")
        body = f"{NOTE}\n{RENDERERS[name](tasks)}\n"

        def fill(m: re.Match[str], body: str = body) -> str:
            return m.group(1) + body + m.group(2)

        text = pattern.sub(fill, text, count=1)
    return text


def next_lines(tasks: list[Task], kind: str | None, limit: int) -> list[str]:
    todo = [t for t in tasks if t.state in ("open", "blocked") and (kind is None or t.kind == kind)]
    todo.sort(key=lambda t: (_urgency(t), t.kind))
    lines = []
    for t in todo[:limit]:
        tag = (
            f"waiting on {', '.join(t.blocked_by)}"
            if t.state == "blocked"
            else (f"p{t.priority}" if t.priority else t.group or "")
        )
        lines.append(f"{t.id:<4} {t.kind:<6} {tag:<12} {t.title}")
    return lines


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0] if __doc__ else None)
    sub = ap.add_subparsers(dest="cmd", required=True)
    nx = sub.add_parser("next", help="open work, most urgent first")
    nx.add_argument("--kind", choices=sorted(KINDS))
    nx.add_argument("--limit", type=int, default=15)
    sub.add_parser("render", help="rewrite the generated blocks in docs/status.md")
    sub.add_parser("check", help="validate the list and check status.md is current")
    args = ap.parse_args(argv)
    try:
        tasks = load()
        if args.cmd == "next":
            print("\n".join(next_lines(tasks, args.kind, args.limit)) or "Nothing open.")
            return 0
        raw = TASKS.read_text(encoding="utf-8")
        current = STATUS.read_text(encoding="utf-8")
        wanted = render_status(current, tasks)
        if args.cmd == "render":
            if canonical(raw) != raw:
                TASKS.write_text(canonical(raw), encoding="utf-8")
                print("docs/plan/tasks.json laid out")
            if wanted != current:
                STATUS.write_text(wanted, encoding="utf-8")
                print("docs/status.md updated")
            return 0
        stale = [
            name
            for name, ok in (
                ("docs/plan/tasks.json", canonical(raw) == raw),
                ("docs/status.md", wanted == current),
            )
            if not ok
        ]
        if stale:
            print(f"Out of date: {', '.join(stale)}. Run `uv run python scripts/tasks.py render`")
            return 1
        print(f"{len(tasks)} items; tasks.json and status.md are current")
        return 0
    except TaskError as e:
        print(e, file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(main())
