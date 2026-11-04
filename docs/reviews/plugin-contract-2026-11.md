# Threat model review: the plugin contract (2026-11-05)

Reviewed by the assistant that wrote the code, so NFR-S2's independent review is still open.

**What changed:** research left the core. The egress process no longer starts research workers, and
the core no longer parses PDFs or holds online-source code. New in the core: plugin keys, the fetch,
artifact and item-key services, and the launcher's plugin rules. Recorded as T-29 to T-31.

**T-25 moved:** a crafted PDF or an altered PaperQA2 pickle is now contained by the plugin's sandbox,
whose only route out is the gateway. The worker's signed cache moved with it.

| Route | Control | Checked |
|---|---|---|
| Direct network from a plugin | Sandbox allows only the gateway | Real sandbox, in all three server modes |
| Writing outside `{data}` | Sandbox; a probe looks for the file from outside | Real sandbox |
| Reading the home folder | Denied, except what the program needs | Real sandbox |
| Fetching a host not listed, or this machine | Refused and audited | Real server and egress |
| The key in another environment | 403 from the gateway | Real server |
| The key at the MCP endpoint | 401 | App test |
| Secrets in an outgoing request | Redaction refuses | App test with a mock transport |
| Changed code | Refused at launch | Real server |
| Risky action after plugin output | Asks, names the plugin, never remembered | Scripted approver, not a chat client |

**Found by this work and fixed:**
- The core's own sandbox blocked fetches to a plugin's hosts in isolated mode. The same limit may have
  affected online research sources in the old isolated core, which were only tested against stand-ins.
- With isolation off everywhere, the server never looked for the sandbox, so plugins couldn't start.
- A plugin's key could use the MCP endpoint, and so other plugins' tools.
- Without egress, fetch didn't check what a name resolves to.
- Unlimited passphrase guesses through the item-key service.

**Open:**
- A plugin may send private text to a listed host when redaction doesn't recognise it (T-29).
- An agent's own network requests after reading plugin output aren't gated (T-30).
- `allow-read` edits the permission list on the user's command, so any program running as the user
  can widen it. That is the same trust level as editing `mcp.toml`.
- The fetch service hasn't been tried against a real outside host.
