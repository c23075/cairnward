# Aider through Cairnward

Status: **tested** (Aider 0.86.2, sandboxed under the Strict preset, with a scripted model): Aider connects, edits files, and every model call is audited; snapshots, diff and revert work on its changes.

Aider sends compact prompts and small diff edits, so it's the best coding agent for CPU-only machines (spec D64).

1. Install Aider. Use Python 3.12: on Python 3.13 one of its dependencies fails to start (`audioop` was removed from Python).
```bash
uv tool install --python 3.12 aider-chat
```
2. Start Cairnward (`uv run cairnward serve`), then in your project:
```bash
uv run --project ~/projects/cairnward cairnward run --profile aider --env coding -- aider
```

What `cairnward run` does for Aider:
- issues a key and an egress session for this run only (revoked when Aider exits);
- points Aider at the gateway: `cw/coding:<effort>` as the main model, `cw/coding:low` as the weak model;
- uses **diff edits** (`AIDER_EDIT_FORMAT=diff`): without it Aider rewrites whole files for models it doesn't know, which costs far more tokens on a CPU;
- turns off Aider's analytics and update check. (LiteLLM inside Aider still tries to download a model price list from `raw.githubusercontent.com`; the sandbox and egress refuse it, and Aider carries on.)
- with isolation on: Aider can write only the project folder, can't read `~/.ssh` or Cairnward's data, and reaches only the gateway and hosts allowed for the environment.

After the run:
```bash
uv run cairnward runs diff <run-id>
```
```bash
uv run cairnward runs revert <run-id>
```

Limits: `--max-requests`, `--max-tokens`, `--max-minutes` stop runaway sessions (or set `[run_budget]` in the environment). Pick a bigger effort with `--model cw/coding:high`.
