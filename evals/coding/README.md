# Coding evaluations

How well do coding agents work **through Cairnward on your hardware**? Each task is a small repository with a failing test; an agent is run on it with `cairnward run` (sandboxed, audited) and the result is checked.

Start Cairnward (`uv run cairnward serve`), then from the repository root:

```bash
uv run python evals/coding/run_eval.py --profile aider
```
```bash
uv run python evals/coding/run_eval.py --profile qwen-code --model cw/coding:high --runs 3
```

Reported per run: pass/fail (tests pass and the test file wasn't edited), wall-clock time, model calls, prompt and completion tokens (from the audit log), and the run ID (`cairnward runs diff <id>` shows what the agent changed).

Default agent commands (non-interactive) can be replaced with `--agent-cmd` using `{prompt}` and `{files}`.

Tasks:
- `fix_failing_test`: `add()` subtracts instead of adding; fix it without touching the tests.
