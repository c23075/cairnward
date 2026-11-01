# ADR-0013: Reuse Anthropic's sandbox-runtime for OS-level isolation

- Status: Accepted
- Date: 2026-11-01
- Spec: §3.2, D45; ADR-0010
- Evidence: [spikes/t06-sandbox](../../spikes/t06-sandbox/README.md) (task T0.6)

## Context
ADR-0010 requires that sandboxed agents, MCP servers and clients have no network except through Cairnward. Task T0.6 compared implementing this ourselves in Python (bubblewrap + network namespace + socket bridges) with reusing Anthropic's open-source **sandbox-runtime** (`srt`, Apache-2.0, npm `@anthropic-ai/sandbox-runtime`), which wraps bubblewrap on Linux (and Seatbelt on macOS, WFP on Windows) with filtering HTTP/SOCKS proxies.

## Findings
Run under srt 0.0.81 on Linux as an unprivileged user (13/13 isolation checks and the chaining test pass; also in CI):

| Behaviour | Result |
|---|---|
| Gateway on the host's loopback, via srt's proxy (IP literal `127.0.0.1:<port>` allow-listed) | works |
| Direct connection to the gateway bypassing the proxy | blocked (private loopback) |
| Direct internet TCP, DNS lookups | blocked |
| Domain not on the allow-list | blocked (403) |
| Reading a denied directory (stand-in for `~/.ssh`) | blocked (hidden) |
| Writing inside the workspace / outside it | works / blocked |
| Writing `.git/hooks` or `.git/config` in the workspace | blocked (srt's mandatory deny paths) |
| Creating any Unix socket; connecting to a host socket (docker.sock, ssh-agent, D-Bus stand-in) | blocked (seccomp) |
| Creating a nested user namespace (escape route for read-only binds) | blocked (seccomp) |
| Traffic srt allows, forwarded to an upstream proxy (stand-in for `cairnward-egress`), which logs and refuses it | works; the refusal reaches the sandboxed program |
| Traffic srt refuses itself | never reaches the upstream proxy |

srt also handles cases our own implementation would have had to discover the hard way: Unix-socket escapes, nested namespaces, git hook/config planting, git-over-SSH through the proxy, JVM proxy settings, a resolved-address check against DNS-rebinding to loopback/link-local/metadata addresses, Ubuntu 24.04's AppArmor restriction, root/`CAP_SETFCAP` handling, and macOS/Windows backends.

Two surprises, both handled by Cairnward:
1. **srt sets `no_proxy=localhost,127.0.0.1,…` inside the sandbox** (its loopback is private on Linux). Clients then try to reach the gateway directly and fail (safely). `cairnward run` must clear `no_proxy`/`NO_PROXY` inside the sandbox and start agents with a clean environment.
2. **srt rejects catch-all allow-lists** (`*`), by design. So srt can't simply forward everything to Cairnward's egress for a decision.

## Options
1. Own Python implementation: no Node dependency; we'd own every subtle escape above and maintain it.
2. Reuse srt via its CLI (settings file + `--control-fd` for live updates).
3. Reuse srt via a small Node shim using its library API (`SandboxManager.initialize(config, askCallback)`), which adds the ask callback for unknown hosts.

## Decision
Reuse srt, pinned to an exact version with a lockfile (integrity hashes), and upgraded deliberately with the spike re-run as a gate.
- **Layering:** srt is the sandbox and first filter; `cairnward-egress` is its parent proxy (srt reads `HTTP(S)_PROXY` from its own environment) and makes the logged, final decision for every connection srt lets through. Gateway traffic (IP literal) goes straight to the gateway.
- **Allow-list:** Cairnward generates srt's allow-list from the effective privacy policy (gateway + `allowlist` hosts + hosts already approved) and pushes changes live through `--control-fd` when the user approves something in the inbox.
- **Ask mode (`network = "ask"`):** start with the CLI (option 2), where unknown hosts are refused by srt and surfaced from srt's debug output as "blocked; approve?" in the inbox. Move to the library shim (option 3) with an `askCallback` that queries Cairnward's approvals API, if the CLI path proves too coarse. Also evaluate `srt proxy` with an external decider (docs/srt-proxy.md in the srt repo) for the egress side in T0.9.
- **Environment:** `cairnward run` starts the agent with a clean environment plus only what its profile needs, and clears `no_proxy`.

## Follow-up (T0.9, 2026-11-01)
Verified with the real `cairnward egress`: srt passes the credentials in its parent `HTTPS_PROXY` URL through, so connections arrive with the session token and are attributed to the right environment/client/run. A host on srt's list but not allowed by Cairnward's policy is refused and audited by Cairnward; a host refused by srt never reaches Cairnward.

## Consequences
- **Node.js ≥ 22.12 is required when `isolation = on`.** Debian 13 ships Node 20, so users install Node 22 (NodeSource packages, nvm/fnm, or the official tarball). `cairnward doctor` checks this; with isolation off, Node isn't needed.
- Linux prerequisites checked by `cairnward doctor`: bubblewrap, socat, ripgrep (srt uses it to find git hooks/config to protect), unprivileged user namespaces (and `kernel.apparmor_restrict_unprivileged_userns=0` on Ubuntu 24.04+), non-root user preferred.
- srt is pre-1.0 (0.0.x) and changes often: pin, read release notes, re-run `spikes/t06-sandbox` (also in CI) before upgrading.
- macOS and Windows sandboxing come "for free" later, subject to their own documented limits.
- Cairnward still runs its own isolation self-tests (`cairnward doctor`), independent of srt, so a regression in srt is caught.
- Supply chain: srt and its 4 runtime dependencies (zod, commander, node-forge, @pondwader/socks5-server) are added to the SBOM and audited (`npm audit` in CI alongside pip-audit).
- **Accepted advisory (2026-11-01):** `npm audit` reports GHSA-mrhx-c7xc-m3w5 (high): zod < 4.6.6 can exhaust memory when validating oversized constrained collections; no fix available in srt 0.0.81. srt uses zod to validate its own configuration, which only Cairnward writes (settings file and `--control-fd`, which srt hides from the sandboxed command), so a sandboxed agent can't feed it input. Impact in our use: none known. CI fails on critical advisories and on any *new* high advisory; this one is re-reviewed at every srt upgrade.
