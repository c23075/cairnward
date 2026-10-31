# ADR-0002: Hybrid build: own core, reuse open-source parts

- Status: Superseded by [ADR-0009](0009-gateway-pivot.md)
- Date: 2026-10-31
- Spec: §3, D1

## Context
The product needs Claude-app-like features *and* strong audit/privacy guarantees. Existing open-source chat UIs (e.g. Open WebUI, LibreChat) already have many features, but their model calls, retrieval, web search and tools each make their own network and file accesses inside a large codebase, so there is no single point where every action can be checked and logged.

## Options
1. Extend an existing UI (Open WebUI): fastest to features; weak audit guarantees; inherits its security model and release pace.
2. Fully custom: full control; much more code (retrieval, parsing, tool protocol) and so more of our own bugs; not more auditable than option 3.
3. Hybrid: own core (agent loop, policy engine, audit log, egress proxy, store, API) plus reused libraries and services (model servers, MCP, Docling, sqlite-vec, restic, rclone).

## Decision
Option 3. Every effect goes through the core's policy engine and audit log; third-party components are called only through that boundary and run sandboxed or out of process where possible.

## Consequences
- Audit coverage is a property of our own (small) core and can be tested exhaustively.
- We build two clients (TUI, web) ourselves.
- External binaries (restic, rclone, Podman/bwrap) need install checks (`cairn doctor`).
