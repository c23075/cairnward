# Progress log

One entry per working session or merged change, newest first (ADR-0022). Say what changed, what
was tested for real and what only against a stand-in, and what was found. Pending work isn't
written here: it goes in the [task list](../../backlog/), and the entry names the task IDs it
touched.

Before 2026-11-24 each session wrote a separate handoff:
[2026-11-10](handoff-2026-11-10.md), [2026-11-05](handoff-2026-11-05.md). They're kept as
history; their open items are now in the task list.

## 2026-11-25: topics and milestones
- The owner asked for categories. Every task now has at least one topic label (16 topics:
  audit, backup, clients, dashboard, decision, deploy, egress, evals, models, performance,
  plugins, release, repo, sandbox, security, wsl2), and checks keep their group label.
- Two milestones: **0.1.0 release** (TASK-4, TASK-5, TASK-6, TASK-36: the release, and the
  repository safety settings to have on before it) and **Production ready** (TASK-1, TASK-2,
  TASK-3, TASK-42 for a restored backup; TASK-11, TASK-67 for WSL2; TASK-9, TASK-39 for an outside
  review), taken from status.md's summary. Topics and milestone choices are a first pass for the
  owner to adjust.
- The CI check now requires a topic on every task, a group label on checks only, and milestones
  that exist. status.md gains a Milestones section and a Topic column; `tasks.py next --topic`.
- Done through the CLI; finish dates kept. Tested: tests/test_tasks.py, 31 tests.

## 2026-11-25: the task list moves to Backlog.md
- The owner chose Backlog.md (option 5 in the revised [ADR-0022](../adr/0022-one-task-list-and-a-progress-log.md)),
  with a CI check. `docs/plan/tasks.json` is gone; tasks are in [backlog/](../../backlog/), one
  file each, created through the Backlog.md CLI so each was checked as it was written.
- The CLI is pinned in `tools/backlog` (Backlog.md 1.57.4, every platform by hash) and Dependabot
  updates it. Its settings keep it local. Traced on 2026-11-25: no outside connections for list,
  view, create, doctor or overview; the board listens only on 127.0.0.1.
- CI's new `tasks` job runs `scripts/tasks.py check`: Backlog.md's `doctor`, Cairnward's rules on
  every task, and whether status.md matches. Backlog.md accepts a hand-edited file with a made-up
  priority, so the rules matter; a test shows the check catching it.
- Every task was compared with its original after the move (type, assignee, priority, status,
  labels, dependencies, finish date, and every word of title and description). Eleven long titles
  were shortened, because the file is named after the title and Windows limits paths; the rest
  of each title moved to its description.
- New: TASK-67, whether the 2026-11-16 WSL2 run already covered I1 (status.md still says it
  hasn't run there).
- Tested: tests/test_tasks.py, 24 tests, 4 of them with the real CLI; `check` on the real list.
  Not yet run on GitHub.

### IDs before 2026-11-25
Each task's notes keep its old ID. Documents written before this change use the old ones.
- **Owner actions:** A1 → TASK-54, A3 → TASK-1, A4 → TASK-2, A5 → TASK-3, A7 → TASK-8, A8 → TASK-4, A10 → TASK-9, A11 → TASK-17, A12 → TASK-18, A13 → TASK-49, A14 → TASK-50, A15 → TASK-51, A16 → TASK-52, A17 → TASK-53, A18 → TASK-10, A19 → TASK-11, A20 → TASK-19, A21 → TASK-5, A22 → TASK-12, A23 → TASK-13, A24 → TASK-20, A25 → TASK-21, A26 → TASK-6, A27 → TASK-14, A28 → TASK-15, A29 → TASK-22, A30 → TASK-7, A31 → TASK-16
- **Agent work:** W1 → TASK-23, W2 → TASK-24, W3 → TASK-56, W4 → TASK-57, W5 → TASK-26, W6 → TASK-29, W7 → TASK-55, W8 → TASK-25, W9 → TASK-27, W10 → TASK-28
- **Checks:** C1 → TASK-61, C2 → TASK-62, C3 → TASK-30, C4 → TASK-66, C5 → TASK-31, C6 → TASK-58, C7 → TASK-63, C8 → TASK-32, C9 → TASK-64, C10 → TASK-59, C11 → TASK-33, C12 → TASK-34, C13 → TASK-35, C14 → TASK-65, C15 → TASK-60, C16 → TASK-36, C17 → TASK-37, C18 → TASK-38, C19 → TASK-39, C20 → TASK-40, C21 → TASK-41, C22 → TASK-42, C23 → TASK-43, C24 → TASK-44, C25 → TASK-45, C26 → TASK-46, C27 → TASK-47, C28 → TASK-48

## 2026-11-24: TLS inspection needs isolation; environments per client (W3, W4)
- The owner approved both proposals. **W3 (I37, D85):** runs in an environment with TLS
  inspection on and isolation off are refused by `cairnward run` and by the core (`run.refused`
  in the audit log), unless the environment sets `allow_without_isolation`, which stays a
  warning. Turning inspection on, or allowing it, is now in the settings snapshot.
- **W4 (I39, D86):** `environments = [...]` per client in `clients.toml`. Other environments get
  403 on chat and embeddings (audited) and on MCP; `/v1/models` lists only allowed ones (a
  run's list is its own environment now too). `client add` limits a new client to the default
  environment; `doctor` warns about unlimited keys.
- Tested: 22 new tests with the real config loader, gateway, management app and audit log; each
  fails with its fix switched off. Backends were fakes. Not run with the real sandbox or a real
  client.
- Found: the default install's `scratch` is more open than `coding`, so `doctor` warns about
  every existing key (A31). MCP refusals aren't audited (W10). Threat model v0.8, T-40, and
  [review](../reviews/threat-model-2026-11-24.md).
- Tasks: W3, W4 done; A31, W10 added.

## 2026-11-24: every pending item in the list (PR 69, second commit)
- The owner asked that anything pending in other docs be in the list. Swept the repository
  checklist, requirements-status.md and limitations.md:
  - A21 (the repository settings) split into one task per setting: A21, A23–A28. The
    checklist's open rows now name their task.
  - A29 and A30: the two proposed ADRs (0021, 0022) waiting for the owner's decision.
  - W8 (the threat-model and limitations check at each milestone, NFR-S2), W9 (the OWASP
    agentic mapping), C28 (the Drive mirror with real rclone, FR-SYNC-2).
  - The other partly done or stand-in requirements were already checks (C3, C5, C13, C20–C27).
- Priority shows as P1–P3, apart from the ID, which never changes.
- `limitations.md` still said WSL2 had never run; corrected (A1 ran it on 2026-11-16).

## 2026-11-24: one task list and a progress log (PR 69)
- Pending work moved into [tasks.json](tasks.json): the owner's A-numbers from status.md, the
  handoff's "next steps" as W1–W7, and status.md's testing checklist as C1–C27.
  `scripts/tasks.py` validates it, prints what's next, and renders status.md's sections from it;
  `tests/test_tasks.py` fails CI when status.md falls behind (ADR-0022, D84).
- New owner tasks from items that were only in prose: A19 (rerun the doctor on the laptop, I42),
  A20 (confirm I15's start time on the desktop), A21 (the repository checklist's GitHub
  settings), A22 (move the server to the research plugin, previously "In progress"). Priorities
  are a first guess; change them in tasks.json.
- W7 (an ADR for the control-plane split) marked done: ADR-0021, 2026-11-11.
- The 2026-11-10 handoff still listed A1 (WSL2) as open after status.md recorded it done on
  2026-11-16. That drift is what this change is for.

## 2026-11-24: AGENTS.md, pre-commit secret scan, Scorecard (PR 68)
- AGENTS.md holds the agent instructions; CLAUDE.md imports it (D83).
- pre-commit runs gitleaks, ruff and file checks. gitleaks found nothing in 278 commits of
  history once three made-up test keys were allowed in `.gitleaks.toml`.
- An OpenSSF Scorecard workflow, weekly and on pushes to `main`. Not yet run.

## 2026-11-14 to 2026-11-19: dependency updates (PRs 50, 51, 53, 57–60)
- Dependabot: pydantic, fastapi, webpki-roots, rustls-pki-types, sha2, the sandbox runtime and
  the browser test's packages.

## 2026-11-16: first real WSL2 run (PR 61)
- A1's checklist ran on the Windows laptop: isolation held, and a Windows chat app worked through
  the gateway ([results](../wsl2.md#results)). Replication (step 7) wasn't tried: C5.
- Found and fixed I42: the doctor's self-test checks the route to Windows. Not yet rerun on the
  laptop: A19.
- `doctor` counts a shared main and fast model once in its memory estimate (found on the laptop,
  where both roles use the same model).

## 2026-11-11: an observer, and managed mode proposed (PR 56)
- Read-only management-socket views for Cairnsteward (D82), tested against two real
  `cairnward serve` instances.
- [ADR-0021](../adr/0021-managed-mode.md), managed mode, proposed. Nothing built.

## 2026-11-10: specification rewritten in plain style (PR 55)
- The spec documents rewritten to simplified technical English rules. No decisions changed.
