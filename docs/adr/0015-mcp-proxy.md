# ADR-0015: MCP tool proxy

- Status: Accepted with amendment (2026-11-02): decision 1 is replaced, see "Amendment" below; decisions 2-6 stand
- Date: 2026-11-01
- Spec: §4.2; FR-MCP-1, FR-MCP-2, FR-MCP-3; threat model T-04, T-07, T-12

## Context
Clients and agents should reach third-party MCP tools only through Cairnward, so every call is policy-checked and audited, servers run with declared permissions, and changed tool definitions ("rug pulls") are caught. The spec's stack table listed the MCP Python SDK.

## Decisions
1. **Own minimal JSON-RPC layer instead of the MCP SDK.** A tool proxy needs initialize, `tools/list` (paged), `tools/call`, ping and a few polite refusals: about 300 lines. Forwarding definitions and results as received (not re-modelled through SDK types) keeps them byte-faithful for hashing and audit, adds no dependencies to the core, and stays easy to audit. Revisit if Cairnward needs resources, prompts, sampling or server-to-client streams.
2. **Transport to clients:** streamable HTTP on the gateway (`/mcp`, `/mcp/<environment>`), JSON responses only (no SSE stream; GET answers 405, which the transport allows), sessions bound to the client's key; `cairnward mcp` is a stdio bridge for clients that only start commands. Origin headers from non-loopback pages are refused.
3. **Naming:** tools appear as `<server>__<tool>`; server names can't contain `_`, so the split is unambiguous and the names fit the strictest client rules (`^[A-Za-z0-9_-]+$`).
4. **Permissions = manifest ∩ environment.** `mcp.toml` declares hosts, writable folders (inside the workspace or the server's own folder, which is also its HOME), optional readable folders and passed-through variables. Network goes only to declared hosts and only through egress, which applies the environment's policy on top; this machine's addresses are dropped unless listed exactly (the sandbox would reach them around egress). Per-tool policy `allow / ask / deny`; unset tools follow the approvals level.
5. **Where servers run.** srt can't nest (tested: bubblewrap refuses a second user namespace), so with the core isolated the networked outer process starts servers from its own copy of the configuration (the core only names server and environment) and hands the core an owner-only Unix socket joined to the server's stdio. In single-process mode the core starts them.
6. **Pinning:** the whole tool definition is hashed; with `mcp_pinning = "on"` only approved hashes are offered and approval is by the exact hashes reviewed (`cairnward mcp approve`, management socket only). Descriptions are scanned for instruction-like text and invisible characters; warnings are shown at review and audited. Optional `pin_file` + `sha256` refuses to start a server whose script or binary changed.

## Consequences
- No dependency on the MCP SDK; protocol versions 2024-11-05 to 2025-11-25 accepted at the tools level.
- Not yet: remote (HTTP) MCP servers, resources/prompts, sampling, built-in Cairnward tools (`lit.*`, `vault.search`) on the same endpoint, redaction of tool arguments and results (comes with the redaction work), stopping idle servers.

## Amendment (2026-11-02, owner review)
Decision 1 is replaced: the protocol plumbing moves to the **official MCP Python SDK** (MIT), so protocol updates (authorisation, transports, new versions) are maintained upstream rather than by Cairnward. FastMCP was considered and not chosen: its proxy re-models definitions and its request flow fits approvals that hold a call open less well, and it brings a larger dependency tree on top of the SDK.
- The SDK may re-serialise definitions, so pinning hashes the **canonical JSON** of each definition (sorted keys, no insignificant whitespace) instead of the bytes received; any change to meaning still changes the hash. Existing pins are migrated once, after checking they still match.
- Policy, approvals, pinning, provenance and audit stay in the hub; only transport and message handling are swapped. The current layer stays until the SDK version passes the same tests (including the end-to-end client tests).
