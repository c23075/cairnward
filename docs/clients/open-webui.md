# Open WebUI through Cairnward

Status: setup checked against the OpenAI API (`tests/test_client_compat.py`, official OpenAI SDK); a hands-on check with Open WebUI itself is pending (task T0.15).

## 1. A key for Open WebUI
```bash
uv run cairnward client add open-webui
```
Keep the printed `cw-...` key; it's shown once.

## 2. Run Open WebUI
Cairnward's gateway only listens on this machine (127.0.0.1), so a container on Docker's normal network can't reach it. Until the container templates arrive (M2, FR-ISO-4), use host networking:
```bash
docker run -d --name open-webui --network=host -v open-webui:/app/backend/data \
  -e OPENAI_API_BASE_URL=http://127.0.0.1:8484/v1 \
  -e OPENAI_API_KEY=cw-PASTE-YOUR-KEY \
  -e ENABLE_OLLAMA_API=false \
  -e ENABLE_OPENAI_API=true \
  -e PORT=3000 \
  -e SCARF_NO_ANALYTICS=true -e DO_NOT_TRACK=true -e ANONYMIZED_TELEMETRY=false \
  ghcr.io/open-webui/open-webui:main
```
Then open http://127.0.0.1:3000.

- `ENABLE_OLLAMA_API=false` matters: otherwise Open WebUI also talks to Ollama directly and those chats bypass Cairnward (no policy, no audit, no vault).
- The telemetry variables switch off Open WebUI's anonymous usage reporting.
- With `--network=host`, Open WebUI itself is **not** sandboxed: Cairnward controls and records its model calls, not its other network traffic (e.g. web search plugins). Turn off features that fetch from the internet if that matters to you.
- Check Open WebUI's current licence terms (it has a branding clause) if you redistribute it.

## 3. Use it
Pick a model such as `cw/coding:low` (fast) or `cw/litreview-offline:high` (30B, thinking on). The model picker is also your effort and environment picker.

Optional: in Open WebUI's settings, turn chat history off or use temporary chats; Cairnward's vault keeps its own (lockable, purgeable) copy.

## 4. Check
```bash
uv run cairnward audit log --last 10
```
You should see `model.call` records with `client:open-webui`.
