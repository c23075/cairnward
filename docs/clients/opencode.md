# opencode through Cairnward

Status: **tested** (opencode 1.19.5, sandboxed under Strict, scripted model): it connects through a per-run configuration and its calls (main and small model) are audited.

```bash
npm install -g opencode-ai
```
```bash
uv run --project ~/projects/cairnward cairnward run --profile opencode --env coding -- opencode
```
The profile writes a configuration for the run only (in the run's private folder, removed afterwards) with a `cairnward` provider, the main and small models, autoupdate off and sharing disabled.

**Network:** at start opencode tries to download its provider package from `registry.npmjs.org`; in the sandbox that's refused and opencode continues with its built-in provider. Nothing needs to be allowed.

**Scripts:** `opencode run "..."` waits for input when stdin isn't a terminal; add `< /dev/null`.
