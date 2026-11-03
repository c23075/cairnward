# Measurements from the running system, 2026-11-03

Taken without running anything new, while other work was using the CPU: read from `/proc`, the
audit log, the vault (lengths only) and CI's coverage report. Timed measurements wait for a quiet
CPU, because their numbers would mean nothing under load.

## Core memory (NFR-P5): meets the target

Target: core ≤ 500 MB without optional workers.

| Process | Current | Peak since start |
|---|---|---|
| Core, in the sandbox | 122 MB | 122 MB |
| Outer process (runs egress) | 114 MB | 114 MB |

Measured after a restart and one review job, with no Presidio. The outer process and core together
are 236 MB. The sandbox's own helper processes aren't counted. **Start time (≤ 3 s) isn't measured
yet.**

## Why one Aider call took 81 minutes: mostly reasoning

From the coding eval in [check-2026-11-03](check-2026-11-03.md), on `cw/coding:medium`, where
reasoning is off:

| Call | Prompt tokens | Reply tokens | Seconds | Reply tokens/s after ~20 tok/s reading | Reasoning share of reply |
|---|---|---|---|---|---|
| First main call | 2,736 | 2,249 | 1,008 | ~2.6 | not separately measured here |
| Fast model (`cw/coding:low`) | 2,481 | 69 | 62 | n/a | none |
| Second main call | 5,252 | 5,689 | 4,834 | ~1.2 | **99.7%** (25,583 of 25,663 characters) |

**Known:** nearly all of the reply was reasoning, with only 72 characters after `</think>`. The
model reasons in the visible reply even with `think: false`.

**Very likely, not proven:** prompt plus reply came to 10,941 tokens in an 8,192-token context, so
the model overran its context. That fits generation running at half the speed of the first call,
because overrunning makes a model server drop text and read again. No separate run confirmed it.

**Not known:** whether turning reasoning off is possible on this model at all, whether a token limit
per request would help or would just cut replies off, and how much of it comes from Aider's prompt.

## Coverage (NFR-MAINT-1): every counted package at 80% or more

Measured locally, without the sandbox tests, the way CI's test job runs.

| Group | Statements | Coverage |
|---|---|---|
| All of `src/` | 13,622 | 78% |
| Counted code (everything but the list below) | 10,431 | 89.7% |

**The rule is per package:** `scripts/check_coverage.py` fails CI if any package is under 80%,
so a strong package can't hide a weak one. New code is counted unless it's added to the list by
name. Before tests were added for this, five packages were short: `approvals_cli` 72%, `dashboard`
74%, `backup` 77%, `run` 79%, `vault_cli` 80%. The closest to the line now are `setup` 81%, `run`
82% and `backup` 83%.

| Left out | Coverage | Why |
|---|---|---|
| `cli.py` | 45% | Argument handling for logic counted elsewhere; much of it needs a live server |
| `core/server.py` | 21% | Runs as a subprocess this job can't measure; `tests/isolation` runs it |
| `research/engines/` | about 50% | Workers with their own Python; `tests/isolation` runs them |
| `check.py`, `doctor.py` | 16%, 55% | Reports about the machine |

Coverage shows a line ran, not that it's right. The left-out code isn't unimportant, either:
`core/server.py` starts the sandbox, and only the isolation job exercises it, without being measured.
