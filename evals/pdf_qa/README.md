# lit.ask evaluation

Synthetic papers with facts on known pages, eight questions (two unanswerable). Run with your configured models, against a running `cairnward serve`:

```bash
uv run python evals/pdf_qa/run_eval.py --env litreview-offline
```

Passing means: every answerable question cites the right file and page and contains the expected fact; both unanswerable questions get "I can't answer this from these sources"; zero invented citations. Record results (model, effort, time per question) in `docs/bench/` when you run it on the reference machine.
