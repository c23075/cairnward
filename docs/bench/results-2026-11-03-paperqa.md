# PaperQA2 on the reference machine, 2026-11-03

`evals/pdf_qa --engine paperqa --only q1`, `qwen3:30b` and `qwen3:1.7b` on CPU (AMD Ryzen 3
3200G, 29.3 GB RAM), through a running isolated `cairnward serve`, worker in the sandbox.

**Result: q1 passed** (right file and page, expected fact, nothing invented) in 397 s, with
one question only. The other seven questions weren't run with this engine.

## Why it failed before
The first real run stopped with `litellm.Timeout` after 181 s (`timeout value=60.0`). The
library behind PaperQA2 (`lmi`) defaults to 60 s per model call with retries, and the worker
didn't set a timeout. It now sets 3,600 s.

From the audit log of the passing run: six evidence summaries took 24 to 35 s each, and the
final answer took 158 s (723 prompt tokens, 524 completion tokens). That last call can't
finish within 60 s, so the old default would have failed it.

Checked against the installed library: its default is 60 s and the worker's setting reaches
the call as 3,600 s.

## Not covered
- This run used the worker file from the fix's own checkout. Each run starts the worker from
  a file on disk, so jobs started by the server get the fix once it's in the server's
  checkout, with no restart. That path wasn't run with the real model here.
- The real-package sandbox tests (`tests/isolation/test_paperqa_engine.py`, scripted model)
  pass with this change.
- Retries are still the library's default (3). A call that really times out would be sent
  again and wait in the queue again.
