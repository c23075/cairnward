# ADR-0010: Enforce egress at the OS level

- Status: Accepted
- Date: 2026-10-31
- Spec: §3.2, D45

## Context
v0.5 routed outbound traffic through an in-process proxy. A Python process can't stop its own libraries from opening sockets (e.g. Docling and Presidio download models on first use), and third-party clients/agents are separate programs Cairnward doesn't control. "Nothing leaves without permission" must hold even when code misbehaves.

## Options
1. In-process proxy plus tests that detect stray connections: simple; not a guarantee.
2. Host firewall rules per process/user: possible (nftables with cgroup/uid matching), but harder to scope per run and to make portable.
3. Network namespaces: sandboxed processes get a namespace with only loopback; Cairnward's endpoints and proxy are bridged in via Unix sockets. The approach used by Anthropic's open-source sandbox-runtime (bubblewrap + socket bridges) and by container internal networks.

## Decision
Option 3 on Linux: bubblewrap with a fresh network namespace for agents and CLI tools; internal-only Podman/Docker networks for server-style clients; Cairnward's own core runs the same way; only the small `cairnward-egress` process has real network access. Whether to reuse sandbox-runtime directly or reimplement its approach in Python is decided in task T0.6. Windows comes later via WSL2 (same mechanism inside WSL2).

## Consequences
- The guarantee no longer depends on any library or tool behaving well.
- Content-level redaction is only possible for traffic Cairnward terminates (model, MCP and research API calls); third-party HTTPS traffic is filtered by host only, unless the user opts into TLS interception (off by default).
- Needs bubblewrap (and user namespaces enabled) on the host; `cairnward doctor` checks this and runs isolation self-tests.
- `cairnward-egress` is small and security-critical: first candidate for a Rust rewrite.
