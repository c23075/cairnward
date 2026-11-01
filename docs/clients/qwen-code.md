# Qwen Code through Cairnward

Status: **tested** (Qwen Code 0.26.5, sandboxed under Strict, scripted model): it connects and its calls are audited.

```bash
npm install -g @qwen-code/qwen-code
```
```bash
uv run --project ~/projects/cairnward cairnward run --profile qwen-code --env coding -- qwen
```
The profile sets `OPENAI_BASE_URL`, `OPENAI_API_KEY` and `OPENAI_MODEL` for the run.

**Privacy:** Qwen Code tries to send usage telemetry to Alibaba Cloud (`*.rum.aliyuncs.com`) and to fetch a model catalogue (`models.dev`). Inside Cairnward's sandbox both are blocked. If you ever run it **without** the sandbox, turn telemetry off in `~/.qwen/settings.json`:
```json
{ "privacy": { "usageStatisticsEnabled": false }, "telemetry": { "enabled": false } }
```

**Speed:** Qwen Code sends a large system prompt with tool definitions and can make more than one model call per prompt, so the first turn is slow on a CPU (minutes). Aider is lighter.
