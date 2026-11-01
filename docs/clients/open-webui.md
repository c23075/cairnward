# Open WebUI through Cairnward

Status: the gateway is checked against the OpenAI API (`tests/test_client_compat.py`, official OpenAI SDK) and the container template with `docker compose config` (`tests/test_containers.py`); a hands-on check with Open WebUI itself is in the [manual checklist](checklist.md).

## 1. A key for Open WebUI
```bash
uv run cairnward client add open-webui
```
Keep the printed `cw-...` key; it's shown once.

## 2. Run Open WebUI (recommended: with no network of its own)
The template in `deploy/containers/open-webui/` puts Open WebUI on an internal-only container network. Its only way out is Cairnward's client socket, joined to `127.0.0.1:8484` inside the container, so every model call goes through Cairnward and nothing else leaves (no web search, no telemetry, no update checks). See [containers.md](containers.md).
```bash
cd deploy/containers/open-webui
```
```bash
cp .env.example .env
```
Fill in `.env` (pinned image versions, your key, the `clients` folder in Cairnward's state folder), then:
```bash
docker compose up -d
```
Open http://127.0.0.1:3000. Document embeddings also go through Cairnward (`RAG_EMBEDDING_ENGINE=openai`).

**Simpler, but not sandboxed:** host networking. Cairnward then controls Open WebUI's model calls, not its other traffic:
```bash
docker run -d --name open-webui --network=host -v open-webui:/app/backend/data \
  -e OPENAI_API_BASE_URL=http://127.0.0.1:8484/v1 -e OPENAI_API_KEY=cw-PASTE-YOUR-KEY \
  -e ENABLE_OLLAMA_API=false -e PORT=3000 -e OFFLINE_MODE=true \
  -e SCARF_NO_ANALYTICS=true -e DO_NOT_TRACK=true -e ANONYMIZED_TELEMETRY=false \
  ghcr.io/open-webui/open-webui:<version>
```
- `ENABLE_OLLAMA_API=false` matters: otherwise Open WebUI also talks to Ollama directly and those chats bypass Cairnward (no policy, no audit, no vault).
- Check Open WebUI's current licence terms (it has a branding clause) if you redistribute it.

### MCP tools
In Open WebUI's settings, add an external tool server of type MCP (streamable HTTP) with URL `http://127.0.0.1:8484/mcp/<environment>` and header `Authorization: Bearer <your key>` (menu names change between versions; see [mcp.md](mcp.md)).

## 3. Use it
Pick a model such as `cw/coding:low` (fast) or `cw/litreview-offline:high` (30B, thinking on). The model picker is also your effort and environment picker.

Optional: in Open WebUI's settings, turn chat history off or use temporary chats; Cairnward's vault keeps its own (lockable, purgeable) copy.

## 4. Check
```bash
uv run cairnward audit log --last 10
```
You should see `model.call` records with `client:open-webui`.
