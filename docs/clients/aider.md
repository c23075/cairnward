# Aider through Cairnward

Aider sends compact prompts, so it's the best coding agent for CPU-only machines (spec D64).

1. Install Aider (anywhere on your PATH):
```bash
uv tool install aider-chat
```
2. Start Cairnward (`uv run cairnward serve`), then in your project:
```bash
uv run --project ~/projects/cairnward cairnward run --profile aider --env coding -- aider
```
`cairnward run`:
- issues a key and an egress session for this run only (revoked when Aider exits);
- points Aider at the gateway with `cw/coding:<effort>` (main) and `cw/coding:low` (weak model);
- turns off Aider's analytics, update check and litellm's price-list download;
- with isolation on: runs Aider in the sandbox: it can write only the project folder, can't read `~/.ssh` or Cairnward's own data, and can reach only the gateway and hosts allowed for the environment.

Pick another effort with `--model cw/coding:high`. Every model call shows up in `cairnward audit log` as `agent:aider` with the run's ID.
