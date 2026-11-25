"""The task list (docs/plan/tasks.json) and the parts of docs/status.md generated from it.

ADR-0022: one list holds all pending work, and status.md shows it. This test is what keeps
them from drifting apart: CI fails until `scripts/tasks.py render` has been run.
"""

from __future__ import annotations

import importlib.util
import json
import sys
from pathlib import Path
from types import ModuleType
from typing import Any

import pytest

SCRIPT = Path(__file__).resolve().parents[1] / "scripts" / "tasks.py"


def load_tasks() -> ModuleType:
    spec = importlib.util.spec_from_file_location("tasks_script", SCRIPT)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module  # dataclasses need the module registered
    spec.loader.exec_module(module)
    return module


tasks = load_tasks()


def item(**over: Any) -> dict[str, Any]:
    base: dict[str, Any] = {
        "id": "A1", "kind": "owner", "priority": 1, "state": "open", "title": "Do it", "detail": "",
    }  # fmt: skip
    return {**base, **over}


# ------------------------------------------------------------------ the real files


def test_the_repository_list_is_valid() -> None:
    assert len(tasks.load()) > 0


def test_tasks_json_is_in_its_fixed_layout() -> None:
    raw = tasks.TASKS.read_text(encoding="utf-8")
    assert tasks.canonical(raw) == raw, "run: uv run python scripts/tasks.py render"


def test_status_md_matches_the_list() -> None:
    current = tasks.STATUS.read_text(encoding="utf-8")
    wanted = tasks.render_status(current, tasks.load())
    assert wanted == current, "run: uv run python scripts/tasks.py render"


def test_check_command_passes_on_the_repository(capsys: pytest.CaptureFixture[str]) -> None:
    assert tasks.main(["check"]) == 0
    assert "are current" in capsys.readouterr().out


# ------------------------------------------------------------------ validation


@pytest.mark.parametrize(
    ("over", "message"),
    [
        ({"kind": "boss"}, "kind must be one of"),
        ({"id": "W1"}, "id is A and a number"),
        ({"state": "maybe"}, "state must be one of"),
        ({"title": " "}, "no title"),
        ({"priority": None}, "needs priority 1, 2 or 3"),
        ({"priority": 7}, "priority must be 1, 2 or 3"),
        ({"state": "done", "done": "24/11/2026"}, "done must be a date"),
        ({"done": "2026-11-24"}, "has a done date but its state is open"),
        ({"state": "blocked"}, "names what blocks it"),
        ({"state": "blocked", "blocked_by": ["A99"]}, "A99, which isn't in the list"),
    ],
)
def test_bad_items_are_refused(over: dict[str, Any], message: str) -> None:
    with pytest.raises(tasks.TaskError, match=message):
        tasks.parse({"items": [item(**over)]})


def test_a_check_needs_a_group() -> None:
    with pytest.raises(tasks.TaskError, match="a check needs a group"):
        tasks.parse({"items": [item(id="C1", kind="check", priority=None)]})


def test_ids_are_unique() -> None:
    with pytest.raises(tasks.TaskError, match="A1: used twice"):
        tasks.parse({"items": [item(), item(title="Again")]})


def test_every_problem_is_reported_at_once() -> None:
    with pytest.raises(tasks.TaskError) as e:
        tasks.parse({"items": [item(kind="boss"), item(id="A2", title="")]})
    assert "kind must be one of" in str(e.value) and "no title" in str(e.value)


def test_done_and_dropped_items_need_no_priority() -> None:
    parsed = tasks.parse(
        {
            "items": [
                item(priority=None, state="done", done="2026-11-24"),
                item(id="A2", priority=None, state="dropped"),
            ]
        }
    )
    assert [t.state for t in parsed] == ["done", "dropped"]


# ------------------------------------------------------------------ rendering


def sample() -> list[Any]:
    rows = [
        item(id="A2", priority=2, title="Second"),
        item(id="A1", priority=1, title="First", detail="Why"),
        item(id="A3", priority=None, state="done", done="2026-11-20", title="Old"),
        item(id="W1", kind="agent", priority=None, state="blocked", blocked_by=["A1"]),
        item(id="W2", kind="agent", priority=3, title="Small"),
        item(id="C1", kind="check", priority=None, group="Real runs", title="Try it"),
        item(id="C2", kind="check", priority=None, group="Real runs", state="done"),
    ]
    rows[3]["title"] = "Later"
    rows[6].update(done="2026-11-01", title="Tried", detail="Passed")
    parsed: list[Any] = tasks.parse({"items": rows})
    return parsed


def test_owner_table_lists_open_items_by_priority_then_done() -> None:
    text = tasks.render_owner(sample())
    assert text.index("| A1 | P1 | First | Why |") < text.index("| A2 | P2 | Second |")
    assert "- **A3** (2026-11-20): Old" in text
    assert "W1" not in text and "C1" not in text


def test_agent_list_puts_blocked_work_last() -> None:
    text = tasks.render_agent(sample())
    assert text.index("**W2** (P3)") < text.index("**W1** (waiting on A1)")


def test_checks_are_grouped_with_open_ones_first() -> None:
    text = tasks.render_checks(sample())
    assert text.startswith("### Real runs")
    assert text.index("- [ ] Try it (C1)") < text.index("- [x] Tried, 2026-11-01: Passed (C2)")


def test_render_replaces_only_the_marked_blocks_and_is_stable() -> None:
    page = (
        "# Status\n\nKept.\n\n"
        "<!-- tasks:owner -->\nstale\n<!-- /tasks:owner -->\n\nAlso kept.\n\n"
        "<!-- tasks:agent -->\n<!-- /tasks:agent -->\n\n"
        "<!-- tasks:checks -->\nold\nlines\n<!-- /tasks:checks -->\n"
    )
    once = tasks.render_status(page, sample())
    assert "stale" not in once and "old\nlines" not in once
    assert "Kept." in once and "Also kept." in once
    assert tasks.render_status(once, sample()) == once


def test_render_refuses_a_page_without_the_markers() -> None:
    with pytest.raises(tasks.TaskError, match="no <!-- tasks:owner --> block"):
        tasks.render_status("# Status\n", sample())


def test_next_lists_open_work_most_urgent_first() -> None:
    lines = tasks.next_lines(sample(), kind=None, limit=10)
    ids = [line.split()[0] for line in lines]
    assert ids[0] == "A1" and ids[-1] == "W1"
    assert "A3" not in ids and "C2" not in ids
    assert [line.split()[0] for line in tasks.next_lines(sample(), "agent", 10)] == ["W2", "W1"]


def test_canonical_layout_is_stable() -> None:
    raw = json.dumps({"about": "x", "items": [item()]})
    once = tasks.canonical(raw)
    assert tasks.canonical(once) == once and once.endswith("\n")
