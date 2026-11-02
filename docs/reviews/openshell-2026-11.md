# Review: NVIDIA OpenShell (November 2026)

Part of M3 T3.1. Reviewed: [NVIDIA/OpenShell](https://github.com/NVIDIA/openshell) at commit `42671c2` (0.1.x line), from its repository documentation (`docs/about/architecture.mdx`, `docs/how-it-works/policies/*`, `docs/how-it-works/inference.mdx`, `docs/about/support-matrix.mdx`) and README. No code was run.

## What it is
A runtime for "fleets of autonomous AI agents": each agent runs in a sandbox whose only network path is a trusted **supervisor** that checks every TCP connection and DNS lookup against a policy, adds credentials only for approved endpoints, and relays the traffic. A **gateway** (control plane) manages sandboxes, policies, credentials ("providers") and access. Written in Rust, with Python, TypeScript, Go and Rust SDKs.

- **Licence:** Apache-2.0 (the same as Cairnward), with a THIRD-PARTY-NOTICES file. Code and ideas can be reused with attribution.
- **Maturity:** 0.1.x with a stable release cadence; the earlier "alpha" label is gone. Still young.
- **Telemetry:** anonymous aggregate counts, **on by default**; off with `OPENSHELL_TELEMETRY_ENABLED=false`, or compiled out. Relevant for anyone using it from a privacy tool.
- **Requirements:** Docker ≥ 28, Podman 5 or KVM (MicroVM) per sandbox; Linux kernel with Landlock ABI ≥ 3 (6.2+), seccomp user notification with `ADDFD_FLAG_SEND`, and `SO_BINDTODEVICE`, all probed before a workload starts (fails closed). The reference machine (Debian 13, kernel 6.12) meets the kernel side; it would need Docker or Podman.

## How it compares with Cairnward's isolation

| | Cairnward (sandbox-runtime, ADR-0013) | OpenShell |
|---|---|---|
| Boundary | bubblewrap namespaces, no network device; HTTP(S)/SOCKS proxy variables point at Cairnward's egress | A container or microVM with networking off; seccomp user notification intercepts every TCP open and DNS query and hands it to the supervisor |
| What's covered | Traffic that goes through the proxy; anything else has no route | Every TCP connection and DNS lookup, any protocol |
| Who made the connection | Not known (the proxy sees a session token, not a program) | The real executable, from trusted `/proc` data; rules list binaries; each binary's hash is recorded and a changed file is denied |
| HTTPS | Host and port only (CONNECT tunnel, no TLS interception) | Terminates TLS by default; rules can match HTTP method, path and query, MCP method and tool name, JSON-RPC method |
| Credentials | Agents get a short-lived run key for Cairnward; backend keys stay in Cairnward | Agents see placeholders; the supervisor substitutes the real credential only on requests to endpoints the provider profile allows |
| Policy changes | Every settings change is audited; weakening is visible (badge, `privacy show`) | Proposed network rules are checked by an SMT-based **prover** for risky new access (new credentialed destinations, new HTTP methods, cloud metadata addresses); a finding blocks auto-approval. A boundary check verifies a policy never exceeds a maximum |
| Losing the control plane | No route out (the proxy is gone); the isolated core stops via its watchdog | The agent is frozen if the supervisor connection drops; reconnect window, then stopped |
| Audit | Hash-chained log with signed anchors, egress report, vault | Logs and an OpenTelemetry trace relay; no tamper-evident log |
| Footprint | A Node package plus bubblewrap, socat, ripgrep; starts in well under a second | A container image per sandbox, a gateway service, Docker/Podman/KVM |
| Scope | One person's machine: gateway, policy, audit, vault, research | Agent runtime only (no model gateway, vault or research) |

## Verdict
- **Don't replace sandbox-runtime now.** OpenShell is stronger at the boundary (every connection, per-program rules, HTTPS inspection, credential substitution), but it needs Docker/Podman or KVM and an image per agent, is young, and has telemetry on by default. For a single CPU-only machine, srt's footprint fits better, and Cairnward's own guarantees (gateway, audit, vault) don't depend on the sandbox type.
- **Worth offering later as an optional second backend** for users who already run Docker or Podman: `cairnward run --sandbox openshell` would start the agent in an OpenShell sandbox whose only allowed destination is Cairnward's gateway, with telemetry switched off. Cairnward stays the policy authority for model and tool calls; OpenShell would enforce the network side. Not scheduled; revisit after M3.

## Ideas to borrow (ranked by value for effort)
1. **Pin the agent's executable** (small). Record the hash of the program `cairnward run` starts (resolving symlinks) the first time it's used in an environment; warn, and with pinning on refuse, when it changes. Cairnward already does this for MCP servers (`pin_file`).
2. **Risk checks on settings changes** (small to medium). Before saving a change, flag access that's risky rather than just weaker: cloud metadata addresses (`169.254.169.254`, `fd00:ec2::254`), wildcards or private ranges in `network_allow_hosts`, hosted backends allowed for environments with locked conversations, isolation switched off while MCP servers with write access are enabled. Plain rules, not a solver; shown in `privacy show` and the dashboard, recorded in the audit log.
3. **Fail closed for runs** (small). When the egress process goes away, stop the runs it served instead of leaving them running without network.
4. **OpenTelemetry for agents** (with T3.9). OpenShell gives agents a reserved collector address that the supervisor answers itself; Cairnward's OpenTelemetry export could accept agent traces the same way through the gateway.
5. **Method and path rules for allowed hosts** (large). Needs TLS termination in egress, with a Cairnward CA trusted inside the sandbox. Belongs with the planned Rust egress proxy, not the current one.
6. **Credential placeholders for MCP servers** (large, same dependency as 5). Until then, connectors that need API keys (academic APIs, T3.4) run inside Cairnward, so keys never reach a tool.
7. **Per-program network rules** aren't possible with srt (the proxy can't see which process connected). They'd come with the OpenShell backend.

Items 1–3 are small enough to do alongside M3; they are listed in [plan/m3.md](../plan/m3.md).
