# Manual client checklist (FR-CLI-1/2)

The automated tests cover the gateway's OpenAI API with the official SDK, the MCP endpoint, the dashboard in Chromium and the container templates' structure. What only a person can check with the real apps, after each Cairnward or client upgrade:

| Check | Open WebUI | Jan | LibreChat |
|---|---|---|---|
| Lists Cairnward's virtual models (`cw/...`) | ☐ | ☐ | ☐ |
| Chat works, streaming shows tokens as they come | ☐ | ☐ | ☐ |
| `cairnward audit log` shows `model.call` with this client's name | ☐ | ☐ | ☐ |
| The conversation appears in `cairnward vault list` / the dashboard | ☐ | ☐ | ☐ |
| Tool calling (a client tool or an MCP tool via Cairnward) works | ☐ | ☐ | ☐ |
| A refused request (e.g. hosted backend under Strict) shows Cairnward's message | ☐ | ☐ | ☐ |
| `X-Cairnward-Warning` (slow prompt, locked conversation) is visible or harmless | ☐ | ☐ | ☐ |
| Container template: no outbound traffic other than through the client socket (`cairnward audit egress` shows nothing new; a web-search plugin fails) | ☐ | n/a | ☐ |
| Telemetry and update checks off (no unexpected hosts in the egress report) | ☐ | ☐ | ☐ |
| Client's own history settings minimised as the guide says | ☐ | ☐ | ☐ |

Record the client version and date in the PR that updates this file.
