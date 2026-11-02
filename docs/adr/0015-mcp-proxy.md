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
- Since done: built-in Cairnward tools (`cairnward__lit_*`, artifacts), skills as prompts and resources, redaction of tool arguments and results (M2), remote HTTP servers (2026-11-02, below). Not done: sampling (refused by design), stopping idle servers.

## Amendment (2026-11-02, owner review)
Decision 1 is replaced: the protocol plumbing is the **official MCP Python SDK** (`mcp` 2.3.0, MIT, pinned and hash-locked), in both directions, so protocol updates (new revisions, authorisation, transports) come from upstream. FastMCP was considered and not chosen (its proxy re-models definitions, fits approvals that hold a call open less well, and adds more dependencies on top of the SDK). Built as:
- **Upstream servers** (`mcp/upstream.py`): the SDK's `ClientSession` over the byte streams Cairnward already has (the sandboxed server's stdio, or the socket the outer process bridges in isolated mode). Lines that aren't JSON (server logs) are skipped. Sampling and elicitation are refused; roots/list answers with no roots.
- **Clients** (`mcp/http.py`): the SDK's low-level `Server` behind its streamable-HTTP session manager, JSON responses, **stateless**: Cairnward's checks (Origin, API key, environment) run on every request first, so server-side sessions would add no protection, only state to exhaust. GET still answers 405.
- **Standard fields only.** The SDK's typed layer keeps only the fields MCP defines; non-standard fields in a tool definition are dropped (nothing extra can reach clients) and recorded once per tool as `mcp.nonstandard` in the audit log.
- **Pins** hash the canonical JSON of the definition as clients get it. Pins from before the switch (format 1, hashed raw definitions) carry over automatically only when the server still sends exactly the definition that was approved; otherwise the tool counts as changed and needs approving again.
- **Behaviour changes:** `initialize` must include `capabilities` and `clientInfo` (as the spec requires); an unknown protocol version is answered with the newest one the SDK supports; results that break their tool's declared output schema are errors.
- **Tests:** the SDK's own client against Cairnward's `/mcp`, Cairnward's client against a server written with the SDK (`tests/test_mcp_conformance.py`), fuzzing of both directions, and the sandboxed end-to-end flow in every server mode.
- **Cost:** 18 more runtime packages (24 → 42 including transitive ones, all hash-locked in uv.lock; among them cryptography, pyjwt, jsonschema, httpx2, sse-starlette). SDK upgrades are deliberate, gated by the full test suite.

## Amendment: remote MCP servers (2026-11-02)
A server in `mcp.toml` can be remote: `url = "https://..."` instead of `command`, with optional `headers` whose secret values must be `${env:NAME}` references (resolved from Cairnward's environment when connecting; never stored in config, never audited). Plain `http` is accepted only for this machine; credentials in the URL are refused; `pin_file`, `env` and `permissions` don't apply (a remote server is limited by the environment's network rules instead).
- **Connection:** the SDK's streamable-HTTP client (`mcp/upstream.py`, `HttpTarget`), redirects only on the same origin (the SDK's rule).
- **Network:** before connecting, the host is checked as a `net.connect` in the environment (network off refuses, ask asks, an allow-list must list it); the traffic then goes through egress under a core session for that environment, so it appears in the egress report and an address on this machine must be listed exactly. In single-process mode the core has one egress session per environment for this; in isolated mode the core's connections go through its sandbox proxy, under the default environment's session.
- **Everything else is as for local servers:** tool definitions pinned and approved, every call policy-checked, redacted and audited, sampling and elicitation refused.
- **Tests:** a real SDK server on loopback with a bearer token from an environment variable (`tests/test_mcp_remote.py`), settings checks, and the same through a running `serve` and egress (`tests/isolation/test_tool_egress.py`). Not tested: a real hosted MCP service, or OAuth (the SDK's OAuth flow isn't wired; servers that require it can't be used yet).
