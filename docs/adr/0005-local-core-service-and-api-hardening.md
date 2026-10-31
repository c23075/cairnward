# ADR-0005: Local core service with thin clients; local API hardening

- Status: Accepted; the thin-client part is superseded by [ADR-0009](0009-gateway-pivot.md) (the API hardening still applies to the dashboard and gateway endpoints)
- Date: 2026-10-31
- Spec: §3, §3.1, D2, D34

## Context
Both a TUI and a web UI are required from day one. Sharing logic means a local service with an API. Local AI servers have been attacked from ordinary web pages via DNS rebinding and cross-site requests to `localhost`.

## Options
1. Each client embeds the core: duplicated logic, two audit paths.
2. Local core daemon; clients are thin and talk only to its API.

## Decision
Option 2, with: Unix domain socket by default (0600); loopback-only HTTP for the web UI with Host allowlist, Origin checks (including WebSocket upgrades), no CORS, `SameSite=Strict` cookies, CSRF tokens, strict CSP, single-use login links; artifact previews on a separate sandboxed origin. The API is versioned and described by a committed OpenAPI document.

## Consequences
- One audit path for both clients.
- Clients can't bypass policy.
- The web UI needs a login step (`cairn web` prints a one-time link).
