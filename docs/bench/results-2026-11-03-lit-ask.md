# lit.ask eval, 2026-11-03

Real runs of `evals/pdf_qa`, lean engine, `qwen3:30b` on CPU (AMD Ryzen 3 3200G, 29.3 GB
RAM). The papers are synthetic. Model output isn't seeded, so runs of the same question
differ.

| Run | Code answering | Questions | Passed | Invented | Failure |
|---|---|---|---|---|---|
| 2026-11-02 (handoff; per-question results not kept) | old, running server | 8 | 6 | 2 | q5, u2: cause not known |
| q5 and u2, 5 times | old, running server | 10 | 10 | 0 | none |
| full | old, running server | 8 | 7 | 1 | u2: false alarm (below) |
| full | old, running server | 8 | 7 | 0 | q1: empty answer (below) |
| q1, 6 times | **new, in-process** | 6 | 6 | 0 | none |
| full | **new, in-process** | 8 | **8** | 0 | none |

Time per question: about 3.5 to 5.5 minutes.

Questions go through the running server, which keeps the code it started with, so the
first four rows all ran the old code. The last two ran the new code in-process with empty
data folders, so they did not cover isolated mode or the gateway's queue. **The fixes reach
a running server only after it is restarted.**

## What was found
- **u2, a false alarm:** a correct refusal quoted a passage word for word without a
  citation, and the checker reported it as "not in the passage cited". A quotation with no
  citation now only has to be found in the papers. Confirmed on the real model: the 8/8 run's
  u2 answer has this shape and passes.
- **Empty answers (q1, 1 of 2 full runs on the old code):** `litreview-offline` defaults to
  `high` effort, which turns reasoning on, and on Ollama the token limit covers reasoning and
  answer together. The limit was 1,024; the model used all of it and returned no text. With
  reasoning on, the limit is now 3,072, and a reply stopped by the limit carries a clear
  warning (`cut_off`) instead of "the answer cites no passage".
- **Fixed from sample answers and tests only, not seen in a real run:** the required refusal
  sentence in quotation marks; a stray quote mark pairing with the next quotation; an invalid
  number in a group, `[1, 9]`, now shown as `[1, ?]`; an unclosed `<think>` in a cut-off reply.

## Still open
- **The higher limit isn't proven by these runs.** The empty answer didn't come back in 7 q1
  runs on the new code, but none of them needed more than 1,024 tokens either (highest 742).
  The new warning was tested only against a fake server using Ollama's reply format.
- **q5's original failure was not explained.** q5 was clean in every rerun (8 of 8), and the
  first run's answers weren't kept. `--save-answers` now keeps them for next time.
- The checker still reports reworded quotations and quoted short phrases that aren't in the
  papers. That is intended, but a harmless quoted phrase can still fail a run.
- Other research steps have the same shared limit with reasoning on: extraction (700 tokens)
  and synthesis (1,200) in `src/cairnward/research/jobs.py`. Not changed, and not yet run
  with real models.
