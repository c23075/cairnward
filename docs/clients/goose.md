# Goose through Cairnward

Status: **untested** (Goose couldn't be installed in the environment used for testing). The profile follows Goose's documented OpenAI-compatible provider settings (`GOOSE_PROVIDER=openai`, `OPENAI_HOST`, `OPENAI_BASE_PATH`, `OPENAI_API_KEY`, `GOOSE_MODEL`).

```bash
uv run --project ~/projects/cairnward cairnward run --profile goose --env coding -- goose session
```
If it works or fails for you, the audit log (`cairnward audit log`) shows whether its model calls arrived.
