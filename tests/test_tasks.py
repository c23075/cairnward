"""The task list in backlog/ (Backlog.md, ADR-0022) and docs/status.md's sections made from it.

Backlog.md checks fields when they're changed through its CLI, not when a file is edited by hand,
so scripts/tasks.py adds Cairnward's rules and checks status.md. CI runs `tasks.py check` with the
pinned CLI (the `tasks` job). Tests marked `needs_cli` run the real CLI and skip without it.
"""

from __future__ import annotations

import importlib.util
import json
import shutil
import subprocess
import sys
from pathlib import Path
from types import ModuleType
from typing import Any

import pytest

ROOT = Path(__file__).resolve().parents[1]


def load_script() -> ModuleType:
    spec = importlib.util.spec_from_file_location("tasks_script", ROOT / "scripts" / "tasks.py")
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module  # dataclasses need the module registered
    spec.loader.exec_module(module)
    return module


tasks = load_script()


def have_cli() -> bool:
    try:
        tasks.backlog_bin()
    except tasks.TaskError:
        return False
    return True


needs_cli = pytest.mark.skipif(
    not have_cli(), reason="Backlog.md CLI not installed (cd tools/backlog && npm ci)"
)

# One task as `backlog task list --json` (schemaVersion 1) prints it, from Backlog.md 1.57.4.
REAL = {
    "id": "TASK-1", "title": "Set up restic to a real remote", "status": "To Do",
    "archived": False, "type": "action", "priority": "high", "project": None,
    "assignees": ["@owner"], "reporter": None, "labels": ["backup"], "milestone": None,
    "parentTaskId": None, "acceptanceCriteriaCompleted": 0, "acceptanceCriteriaCount": 0,
    "dependencies": [], "references": [], "modifiedFiles": [], "ordinal": 1000,
    "createdAt": "2026-11-24T15:57:24Z", "updatedAt": None, "dueDate": None, "isReady": True,
}  # fmt: skip
CFG = tasks.Settings(
    statuses=("To Do", "In Progress", "Done"),
    types=("action", "work", "check"),
    priorities=("High", "Medium", "Low"),
    labels=("could-reveal-bugs", "never-run-for-real", "project-level", "stand-in-only",
            "backup", "release", "security"),
    milestones={"m-0": "0.1.0 release", "m-1": "Production ready"},
)  # fmt: skip


def task(description: str = "", **over: Any) -> Any:
    return tasks.from_json({**REAL, **over}, description)


# ------------------------------------------------------------------ Cairnward's rules


def test_a_good_list_has_no_problems() -> None:
    good = [
        task("Why", id="TASK-1"),
        task(id="TASK-2", type="work", assignees=["@agent"], priority="low"),
        task(id="TASK-3", type="check", assignees=[], priority=None,
             labels=["project-level", "release"], milestone="m-0"),
        task(id="TASK-4", type="work", assignees=["@agent"], priority=None,
             dependencies=["TASK-1"]),  # waiting, so no priority needed
        task(id="TASK-5", status="Done", priority=None, updatedAt="2026-11-20T00:00:00Z"),
    ]  # fmt: skip
    assert tasks.problems(good, CFG) == []


@pytest.mark.parametrize(
    ("over", "message"),
    [
        ({"status": "Blocked"}, "status 'Blocked' isn't one of"),
        ({"type": None}, "type must be one of action, work, check, not None"),
        ({"type": "bug"}, "type must be one of"),
        ({"priority": "urgent"}, "priority 'urgent' isn't one of"),
        ({"labels": ["misc"]}, "label 'misc' isn't configured"),
        ({"assignees": ["@agent"]}, "an action is the owner's"),
        ({"type": "work"}, "work is an agent's"),
        ({"priority": None}, "need a priority"),
        ({"labels": []}, "needs at least one topic label"),
        ({"labels": ["project-level"]}, "needs at least one topic label"),
        ({"type": "check", "assignees": [], "priority": None}, "exactly one group label"),
        (
            {
                "type": "check",
                "assignees": [],
                "priority": None,
                "labels": ["backup", "project-level", "stand-in-only"],
            },
            "exactly one group label",
        ),
        ({"labels": ["backup", "project-level"]}, "only checks have a group label"),
        ({"milestone": "m-9"}, "milestone m-9 doesn't exist"),
        ({"dependencies": ["TASK-99"]}, "depends on TASK-99, which isn't in the list"),
    ],
)
def test_each_rule(over: dict[str, Any], message: str) -> None:
    found = tasks.problems([task(**over)], CFG)
    assert any(message in p for p in found), found


def test_problems_name_the_task_and_all_are_reported() -> None:
    found = tasks.problems([task(id="TASK-7", type="bug", priority="urgent")], CFG)
    assert len(found) == 2 and all(p.startswith("TASK-7 (Set up restic") for p in found)


# ------------------------------------------------------------------ rendering


def sample() -> list[Any]:
    return [
        task("Second why", id="TASK-2", priority="medium", title="Second"),
        task("Why | with a pipe", id="TASK-1", title="First"),
        task("Old", id="TASK-3", status="Done", priority=None, updatedAt="2026-11-20T05:00:00Z"),
        task("", id="TASK-4", type="work", assignees=["@agent"], priority=None,
             dependencies=["TASK-1"], title="Later"),
        task("See [ops](/docs/operations.md)", id="TASK-5", type="work", assignees=["@agent"],
             priority="low", title="Small"),
        task("", id="TASK-6", type="check", assignees=[], priority=None,
             labels=["never-run-for-real", "release"], title="Try it", milestone="m-0"),
        task("Passed", id="TASK-7", type="check", assignees=[], priority=None, status="Done",
             labels=["never-run-for-real", "release"], title="Tried",
             updatedAt="2026-11-01T00:00:00Z", milestone="m-0"),
    ]  # fmt: skip


def test_owner_table_is_by_priority_then_done_newest_first() -> None:
    text = tasks.render_owner(sample())
    assert text.index("| TASK-1 | High | backup | First | Why \\| with a pipe |") < text.index(
        "| TASK-2 | Medium | backup | Second | Second why |"
    )
    assert "- **TASK-3** (2026-11-20): Set up restic to a real remote. Old" in text
    assert "TASK-4" not in text and "TASK-6" not in text


def test_agent_list_puts_waiting_work_last_and_fixes_links() -> None:
    text = tasks.render_agent(sample())
    assert text.index("**TASK-5** (Low; backup): Small. See [ops](operations.md)") < text.index(
        "**TASK-4** (waiting on TASK-1; backup): Later"
    )


def test_milestones_count_done_and_list_what_is_left() -> None:
    text = tasks.render_milestones(sample(), CFG)
    assert text == "- **0.1.0 release**: 1 of 2 done. Still to do: TASK-6"


def test_next_can_show_one_topic() -> None:
    mixed = [task(id="TASK-1"), task(id="TASK-2", labels=["release"])]
    assert [line.split()[0] for line in tasks.next_lines(mixed, None, 10, "release")] == ["TASK-2"]


def test_a_dependency_that_is_done_no_longer_blocks() -> None:
    done_first = [task(id="TASK-1", status="Done", priority=None), sample()[3]]
    assert "(waiting on" not in tasks.render_agent(done_first)


def test_checks_are_grouped_open_first() -> None:
    text = tasks.render_checks(sample(), CFG)
    assert text.startswith("### Never run for real")
    assert text.index("- [ ] Try it (TASK-6)") < text.index(
        "- [x] Tried, 2026-11-01: Passed (TASK-7)"
    )


def test_links_from_the_root_work_from_docs() -> None:
    assert tasks.for_docs("[a](/docs/x.md) [b](/README.md) [c](https://x.org)") == (
        "[a](x.md) [b](../README.md) [c](https://x.org)"
    )


def test_render_replaces_only_marked_blocks_and_is_stable() -> None:
    page = (
        "# Status\n\nKept.\n\n<!-- tasks:milestones -->\n<!-- /tasks:milestones -->\n\n"
        "<!-- tasks:owner -->\nstale\n<!-- /tasks:owner -->\n\nAlso kept.\n\n"
        "<!-- tasks:agent -->\n<!-- /tasks:agent -->\n\n<!-- tasks:checks -->\nold\n"
        "<!-- /tasks:checks -->\n"
    )
    once = tasks.render_status(page, sample(), CFG)
    assert "stale" not in once and "Kept." in once and "Also kept." in once
    assert tasks.render_status(once, sample(), CFG) == once
    with pytest.raises(tasks.TaskError, match="no <!-- tasks:milestones --> block"):
        tasks.render_status("# Status\n", sample(), CFG)


def test_next_lists_open_work_most_urgent_first() -> None:
    ids = [line.split()[0] for line in tasks.next_lines(sample(), None, 10)]
    assert ids[0] == "TASK-1" and ids[-1] == "TASK-4"
    assert "TASK-3" not in ids and "TASK-7" not in ids


def test_load_reads_descriptions_and_refuses_an_unknown_schema() -> None:
    def fake(args: list[str]) -> str:
        if args[:2] == ["task", "list"]:
            return json.dumps({"schemaVersion": 1, "kind": "task-list", "tasks": [REAL]})
        return json.dumps({"schemaVersion": 1, "kind": "task-view", "task": {"description": "Why"}})

    assert tasks.load(fake)[0].description == "Why"
    with pytest.raises(tasks.TaskError, match=r"unexpected Backlog\.md JSON schema 2"):
        tasks.load(lambda args: '{"schemaVersion": 2, "tasks": []}')


# ------------------------------------------------------------------ the real CLI


@pytest.fixture
def scratch(tmp_path: Path) -> Any:
    """A backlog made by the real CLI with Cairnward's configuration."""
    subprocess.run(["git", "init", "-q"], cwd=tmp_path, check=True)
    (tmp_path / "backlog").mkdir()
    shutil.copy(ROOT / "backlog" / "config.yml", tmp_path / "backlog" / "config.yml")
    return tasks.cli_in(tmp_path)


@needs_cli
def test_real_cli_round_trip(scratch: Any) -> None:
    scratch(["milestone", "add", "0.1.0 release"])
    scratch(["task", "create", "Back up", "--type", "action", "-a", "@owner", "-l", "backup",
             "--priority", "high", "-d", "Why", "-m", "0.1.0 release", "--plain"])  # fmt: skip
    scratch(["task", "create", "Test it", "--type", "work", "-a", "@agent", "-l", "backup",
             "--dep", "TASK-1", "--plain"])  # fmt: skip
    loaded = sorted(tasks.load(scratch), key=lambda t: t.number)
    assert [(t.id, t.type, t.description) for t in loaded] == [
        ("TASK-1", "action", "Why"), ("TASK-2", "work", "")]  # fmt: skip
    cfg = tasks.settings(scratch)
    assert cfg.milestones == {"m-0": "0.1.0 release"} and loaded[0].milestone == "m-0"
    assert tasks.problems(loaded, cfg) == []
    tasks.doctor(scratch)


@needs_cli
def test_real_cli_refuses_bad_values_but_hand_edits_need_our_check(
    scratch: Any, tmp_path: Path
) -> None:
    with pytest.raises(tasks.TaskError, match="Invalid priority"):
        scratch(["task", "create", "x", "--priority", "urgent", "--plain"])
    scratch(["task", "create", "Back up", "--type", "action", "-a", "@owner",
             "--priority", "high", "--plain"])  # fmt: skip
    path = next((tmp_path / "backlog" / "tasks").glob("task-1 - *.md"))
    path.write_text(path.read_text().replace("priority: high", "priority: urgent"))
    found = tasks.problems(tasks.load(scratch), tasks.settings(scratch))
    assert any("priority 'urgent'" in p for p in found)


@needs_cli
def test_real_doctor_catches_a_duplicate_id(scratch: Any, tmp_path: Path) -> None:
    scratch(["task", "create", "One", "--plain"])
    path = next((tmp_path / "backlog" / "tasks").glob("task-1 - *.md"))
    shutil.copy(path, path.with_name("task-1 - copy.md"))
    with pytest.raises(tasks.TaskError, match="doctor failed"):
        tasks.doctor(scratch)


@needs_cli
def test_the_repository_follows_the_rules_and_status_md_is_current(
    capsys: pytest.CaptureFixture[str],
) -> None:
    assert tasks.main(["check"]) == 0, capsys.readouterr().err
