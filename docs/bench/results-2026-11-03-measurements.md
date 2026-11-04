# Measurements on the reference machine, 2026-11-03

AMD Ryzen 3 3200G (4 threads, 29 GB RAM, no GPU), ext4, nothing else running: real `cairnward serve`
processes on their own ports and folders, created fresh for each measurement. Requests come from a
real client over TCP. Overhead uses a stand-in model server that answers at once; the rest uses real
Ollama. Figures are medians.

## Against the targets

| Requirement | Target | Isolated (the normal setup) | Not isolated | Result |
|---|---|---|---|---|
| NFR-P5 core memory | ≤ 500 MB | 122 MB | not measured | **meets** |
| NFR-P5 start time | ≤ 3 s | **5.3 s** | 2.7 s | **misses** when isolated |
| NFR-P2 overhead, Strict | ≤ 50 ms | **105 ms** | 59 ms | **misses** |
| NFR-P2 overhead, all off | ≤ 10 ms | not applicable (isolation is a protection) | 7 ms | **meets** |
| NFR-P6 first token, fast model | ≤ 15 s | 11 to 14 s | 10 s direct, no gateway | **meets** |
| NFR-P6 first token, main model | ≤ 30 s | 22 s | 21 s direct, no gateway | **meets** |
| NFR-P3 cache reuse | ≥ 10× | **19.6×** (95 s to 4.8 s, 2,048 tokens) | not measured | **meets** |

First-token times use a 512-token prompt with the model already loaded; loading the main model first
adds about 70 s. One of three fast-model runs through the gateway generated far more slowly than the
others (19 tokens in 30 s); its first token still met the target, and the cause wasn't looked into.

**Start time, later on 2026-11-04.** Python starts twice with isolation on, and the outer process was
importing everything the core needs. Those imports now happen only in the core. On the same machine
and script, with empty data folders, time until `/health` answers went from about 3.6 s to a median
of **2.8 s** (2.6 to 3.0, five runs). That meets the 3 s target, but this script read 3.6 s where the
table's method read 5.3 s, so the two aren't directly comparable, and a large existing data folder
wasn't tried. What's left is mostly the core's own imports (about 1.3 s) and Python starting twice;
opening the databases and checking migrations takes under 150 ms, and the sandbox itself about 0.3 s.

## Where Strict's overhead goes

Time for a chat request, with a stand-in that answers in under 1 ms:

| | No audit or vault | Audit only | Vault only | Strict |
|---|---|---|---|---|
| Not isolated | 8 ms | 48 ms | 22 ms | 59 ms |
| Isolated | 60 ms | 95 ms | 71 ms | 107 ms |

- **Audit writes:** about 35 to 40 ms, waiting for the disk so the record exists before the effect.
- **The sandbox's route to the model server:** about 50 ms. Each chat makes two calls out (the memory
  check and the chat), through the sandbox's proxy and egress. `/health`, which makes neither, takes
  1 ms in both modes, so the relay in front of the core isn't the cost.
- **Vault writes:** about 10 to 15 ms.

Each of the first two is itself about the size of the whole target. Meeting 50 ms would mean
weakening the audit guarantee or changing how the core reaches models. A model call here takes
minutes, so 100 ms can't be noticed. **Proposed: change the target, not the code.**

## Why one Aider call took 81 minutes: mostly reasoning

From the coding eval in `check-2026-11-03.md` (on the `dev` branch), on `cw/coding:medium`, where
reasoning is off:

| Call | Prompt tokens | Reply tokens | Seconds | Reply tokens/s after ~20 tok/s reading | Reasoning share of reply |
|---|---|---|---|---|---|
| First main call | 2,736 | 2,249 | 1,008 | ~2.6 | not separately measured here |
| Fast model (`cw/coding:low`) | 2,481 | 69 | 62 | n/a | none |
| Second main call | 5,252 | 5,689 | 4,834 | ~1.2 | **99.7%** (25,583 of 25,663 characters) |

**Known:** nearly all of the reply was reasoning, with only 72 characters after `</think>`.

**Tested directly on Ollama, `qwen3:30b`, two prompts, three tries each:**

| Setting | Reasoning in the answer | Tokens |
|---|---|---|
| `think: false` | yes, 6 of 6 | 182 to 823 |
| `think: false` and `/no_think` in the message | yes, 6 of 6 | 90 to 485 |
| `think: true` | no, 0 of 6; comes back separately | 149 to 295 |
| not set | no, 0 of 6; comes back separately | 218 to 602 |

So this model can't stop reasoning. "Off" only moves it into the answer. **Fix:** `medium` effort
now asks for reasoning, so clients get only the answer. `low` stays off, because the fast model's
replies were short with it off. A model without thinking support may refuse `medium` as it already
does `high`.

**The fix on the real thing:** the same coding task, Aider, Balanced preset, isolated server.

| | Before | After (3 runs) |
|---|---|---|
| Result | passed | passed, 3 of 3 |
| Time | 5,926 s | **174, 221 and 405 s** |
| Main-model calls | 2 | 1 each |
| Reply tokens in the last main call | 5,689 | 651 to 792 |
| Stray reasoning in the answer | 25,583 characters | none |

The answers were 94 characters with one edit block, and reasoning came back separately (about 2,500
to 3,100 characters). "Before" is one run, and how long this model reasons varies a lot, so the size
of the difference is rough. It's also one small task: long tasks may still overrun the context.

**Very likely, not proven:** prompt plus reply came to 10,941 tokens in an 8,192-token context, so
the model overran its context. That fits generation running at half the speed of the first call,
because overrunning makes a model server drop text and read again. No separate run confirmed it.

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
