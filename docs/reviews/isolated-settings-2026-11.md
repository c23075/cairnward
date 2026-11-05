# Threat model review: dashboard settings on an isolated server (2026-11-05)

Reviewed by the assistant that wrote the code, so NFR-S2's independent review is still open.

**What changed:** an isolated core can't write the configuration folder, so dashboard edits now go to
the outer process over the control socket, through `POST /v1/settings/{kind}`. This is a new request
across the sandbox boundary. Recorded as T-32. The core also gets a temporary folder for decrypted
locked artifacts, memory-backed where `XDG_RUNTIME_DIR` exists, and otherwise on disk in the state
folder. It's cleared when the core starts.

| Route | Control | Checked |
|---|---|---|
| Editing allow-lists, backends or MCP servers | The endpoint only accepts `privacy` and `backup` edits | Code read; a real run covers allowed edits only |
| An edit that breaks the configuration | Refused unless the whole configuration still loads | Unit tests of the edit code |
| Changing a setting from the dashboard | Works through the outer process | Real isolated server (`tests/isolation/test_owner_flows.py`) |

**Accepted:** a compromised core can pass `confirm`, because the dashboard's confirmation happens inside
the core, so it can weaken a listed privacy setting. That takes effect on restart and is audited then
by a process the core doesn't control. It's visible, but it isn't prevented.
