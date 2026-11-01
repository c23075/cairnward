# Jan through Cairnward

Status: same OpenAI API as the tested SDK; hands-on check pending.

1. Create a key:
```bash
uv run cairnward client add jan
```
2. In Jan: **Settings → Model Providers → OpenAI-compatible** (wording varies by version): base URL `http://127.0.0.1:8484/v1`, API key `cw-...`. Pick a `cw/<environment>:<effort>` model.
3. Turn off Jan's own local model engine for these chats, or they bypass Cairnward; turn off analytics in Jan's privacy settings.
4. Jan runs unsandboxed; Cairnward controls and records its model calls only.
