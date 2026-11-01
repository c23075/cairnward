# LibreChat through Cairnward

LibreChat (MIT) is the third tested client (FR-CLI-2): a server-style chat app that supports custom OpenAI-compatible endpoints and MCP servers.

Status: configuration and the container override are checked with `docker compose config` (`tests/test_containers.py`); a hands-on check is in the [manual checklist](checklist.md).

## 1. A key
```bash
uv run cairnward client add librechat
```

## 2. librechat.yaml
```yaml
version: 1.2.1   # use the config version your LibreChat release documents
endpoints:
  custom:
    - name: "Cairnward"
      apiKey: "${CAIRNWARD_KEY}"
      baseURL: "http://127.0.0.1:8484/v1"
      models:
        default: ["cw/chat:medium", "cw/chat:low", "cw/coding:medium"]
        fetch: true          # lists Cairnward's virtual models
      titleConvo: true
      titleModel: "cw/chat:low"
mcpServers:
  cairnward:
    type: streamable-http
    url: "http://127.0.0.1:8484/mcp/chat"
    headers:
      Authorization: "Bearer ${CAIRNWARD_KEY}"
```
Remove LibreChat's other providers (OpenAI, Anthropic, ...) from its `.env` so every chat goes through Cairnward.

## 3. Run it without a network of its own
Copy `deploy/containers/librechat/docker-compose.override.yml` next to LibreChat's `docker-compose.yml` and add to LibreChat's `.env`: `CAIRNWARD_KEY`, `CAIRNWARD_CLIENT_SOCKETS` (`<state folder>/clients`) and `SOCAT_IMAGE` (pinned). Every LibreChat service moves to an internal-only network; the API and RAG services reach Cairnward at `127.0.0.1:8484` through the client socket (embeddings too); the UI is published on 127.0.0.1:3080. See [containers.md](containers.md).

## 4. Privacy notes
- LibreChat keeps conversations in its MongoDB; Cairnward's vault keeps its own copy (lockable, purgeable). Deleting in one doesn't delete in the other.
- Its web search and file-fetching features can't reach the internet from the internal network.
