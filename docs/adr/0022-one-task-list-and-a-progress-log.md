# ADR-0022: One task list (Backlog.md) and a progress log

- Status: Proposed
- Date: 2026-11-24, revised 2026-11-25 to choose Backlog.md
- Spec: D84

## Context
Pending work was written down in four places, by hand:
- `docs/status.md`: the owner's actions (A-numbers), an "In progress" table and a testing
  checklist;
- the newest `docs/plan/handoff-*.md`: "next steps for a session", which agents read first;
- `docs/plan/requirements-status.md`: each requirement's state;
- GitHub issues, for outside reports.

They drifted apart. On 2026-11-24 the newest handoff (2026-11-10) still listed A1 (WSL2) as open,
although status.md had recorded it done on 2026-11-16, and it said nothing about D82 or ADR-0021
(2026-11-11). An agent asked "what's next?" answers from the handoff, so it could be two weeks
behind. Each session also wrote a new handoff file, and the open items had to be copied forward
each time.

Anthropic's harness for long-running agents keeps state in a feature list and a progress file
that every session reads at the start, with the git log. Its list is JSON because models tend to
rewrite Markdown lists; the agent changes only an item's state.

Several agents work on this repository (Claude Code, Codex, OpenCode and others), so the list has
to work the same way for each, and enforce its fields rather than rely on each agent editing a
file correctly.

## Options
1. **Keep the files, add a checklist** to AGENTS.md so sessions update all of them. Cheap, but
   nothing checks it, and the duplication stays.
2. **GitHub Issues and a GitHub Project.** The industry standard, with a board and fields. But an
   agent needs the network and a token, the owner's personal actions (disk encryption, a backup
   password) don't belong in a public tracker, and CI can't easily check status.md against it.
3. **Beads**, a git-backed tracker for agents. Good at dependencies, but a separate program and
   database, and its issues don't show on GitHub.
4. **A JSON list with a Cairnward script** (this ADR's first version, PR 69). No dependency,
   CI-checked, but agents edit the file by hand, so the rules are enforced only at review.
5. **Backlog.md** (MIT): one Markdown file per task in `backlog/`, a CLI and an MCP server that
   check fields when a task is changed, JSON output for scripts, and a local Kanban board. Agents
   change tasks through tools instead of editing files, the same way in every agent. It needs
   Node and a 120 MB binary per platform, and it doesn't check hand-edited files.

## Decision
Option 5, Backlog.md, with Cairnward's own CI check on top.
- **`backlog/`** holds every piece of pending work, one file per task (`TASK-n`). The type says
  whose it is: `action` (the owner's, assignee `@owner`), `work` (an agent's, `@agent`) or
  `check` (something to run for real, labelled with its group). Priority is High, Medium or Low.
  A task waiting on another names it as a dependency. Finished tasks are `Done`.
- **Changes go through the CLI or its MCP tools** (`backlog task create | edit`), which refuse
  values that aren't configured. `backlog browser` shows the board on 127.0.0.1.
- **The CLI is pinned** in `tools/backlog` (package-lock.json with each platform's hash) and
  updated by Dependabot. Its settings keep it local: no remote git operations, no automatic
  commits, git hooks never bypassed. On 2026-11-25, tracing its system calls showed no outside
  connections for list, view, create, doctor or overview, and the board listening only on
  127.0.0.1.
- **CI** (the `tasks` job) installs the pinned CLI and runs `scripts/tasks.py check`:
  Backlog.md's `doctor` (duplicate IDs, dependency loops), then Cairnward's rules on every task
  (configured values, type and assignee agree, open work has a priority unless it waits on
  another task, a check has one group), then whether status.md's generated sections match. So a
  hand-edited file is caught at the pull request.
- **`docs/plan/progress.md`** replaces the dated handoffs: one entry per session or merged
  change, newest first. It records what happened; it doesn't hold pending work.
- **Sessions** start with the git log, the top of the progress log and the open tasks, and end by
  updating tasks, rendering status.md, and adding an entry (AGENTS.md).

Unchanged: known issues (I-numbers) stay in status.md as a register, and their link to GitHub
issues stays as CONTRIBUTING.md describes. requirements-status.md stays as the per-requirement
reference. An issue that needs work gets a task that names it.

## Consequences
- One answer to "what's next", the same for every agent, with a board for the owner.
- Task IDs changed from A/W/C numbers to `TASK-n` on 2026-11-25. Each task's notes keep its old ID,
  and the progress log has the full map. Older documents keep the old IDs.
- A new tool to keep current. If Backlog.md's file or JSON format changes, `tasks.py` and its
  tests catch it in the Dependabot PR that updates it.
- The repository checklist in `repository-management.md` is still its own list of settings; each
  open row names its task.
- Reversible: tasks are plain Markdown files, and status.md keeps its sections.
- If the project gains contributors, GitHub Issues may become the list (option 2), with the
  owner's private actions kept here. That would be a new ADR.
