# Progress log

One entry per working session or merged change, newest first (ADR-0022). Say what changed, what
was tested for real and what only against a stand-in, and what was found. Pending work isn't
written here: it goes in [tasks.json](tasks.json), and the entry names the task IDs it touched.

Before 2026-11-24 each session wrote a separate handoff:
[2026-11-10](handoff-2026-11-10.md), [2026-11-05](handoff-2026-11-05.md). They're kept as
history; their open items are now in the task list.

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
