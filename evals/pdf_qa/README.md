# lit.ask evaluation

Synthetic papers with facts on known pages, eight questions (two unanswerable). Run with your configured models, against a running `cairnward serve`:

```bash
uv run python evals/pdf_qa/run_eval.py --env litreview-offline
```

Passing means: every answerable question cites the right file and page and contains the expected fact; both unanswerable questions get "I can't answer this from these sources"; zero invented citations. Record results (model, effort, time per question) in `docs/bench/` when you run it on the reference machine.

Model answers vary between runs and are otherwise only shown in the terminal when something was invented. To keep them for a failure that may not come back, add `--save-answers answers.jsonl` (appends each full answer and its score; not part of any report). `--only q5,u2` reruns chosen questions.

**After changing Cairnward's code, restart `cairnward serve` first.** Questions go through the running server, which keeps using the code it started with. To test new code without restarting, give the eval its own empty folders, so it runs in-process and leaves your vault and audit log alone (isolation and the queue don't apply):

```bash
export CAIRNWARD_DATA_DIR=$(mktemp -d) CAIRNWARD_STATE_DIR=$(mktemp -d)
uv run python evals/pdf_qa/run_eval.py
```
