# ADR-0022: One task list and a progress log

- Status: Proposed
- Date: 2026-11-24
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

Anthropic's harness for long-running agents keeps state in a JSON feature list and a progress
file that every session reads at the start, with the git log. The list is JSON because models
tend to rewrite Markdown lists; the agent changes only an item's state.

## Options
1. **Keep the files, add a checklist** to AGENTS.md so sessions update all of them. Cheap, but
   nothing checks it, and the duplication stays.
2. **GitHub issues as the one list.** Standard, and has a UI. But an agent needs the network and
   a token to read it, it can't be checked in CI against status.md without the API, and the
   owner's personal actions (disk encryption, a backup password) don't belong in a public
   tracker.
3. **Beads**, a git-backed tracker for agents. Built for this, but another tool (a Go binary and
   a database) for one maintainer, and status.md would still be written by hand.
4. **A JSON list in the repository, with status.md generated from it.** One file, readable
   offline, reviewed in PRs like code; a test fails CI when status.md falls behind.

## Decision
Option 4.
- **`docs/plan/tasks.json`** holds every piece of pending work: owner actions (A), agent work
  (W) and checks to run for real (C), each with a state (open, blocked, done, dropped), a
  priority for open work, and a date when done.
- **`scripts/tasks.py`** validates it, prints what's next (`next`), and renders three sections
  of status.md between markers (`render`). `tests/test_tasks.py` fails when they don't match.
- **`docs/plan/progress.md`** replaces the dated handoffs: one entry per session or merged
  change, newest first. It records what happened; it doesn't hold pending work.
- **Sessions** start by reading the git log, the top of the progress log and
  `scripts/tasks.py next`, and end by updating tasks.json, rendering, and adding an entry
  (AGENTS.md).

Unchanged: known issues (I-numbers) stay in status.md as a register, and their link to GitHub
issues stays as CONTRIBUTING.md describes. requirements-status.md stays as the per-requirement
reference. An issue that needs work gets a task that names it.

## Consequences
- One answer to "what's next", and CI catches a status page that wasn't updated.
- The owner edits a JSON file, or asks an agent to, instead of a Markdown table.
- The repository checklist in `repository-management.md` is still its own list of settings;
  A21 points to it rather than copying each row.
- Reversible: status.md keeps its sections, so dropping the markers and the test returns to
  hand-written pages.
- If the project gains contributors, GitHub issues may become the list (option 2), with
  tasks.json kept for the owner's private actions or generated from the issues. That would be a
  new ADR.
