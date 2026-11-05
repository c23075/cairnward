# ADR-0018: OpenShell as a second sandbox, and TLS inspection in egress

- Status: Accepted
- Date: 2026-11-05
- Spec: §3.2, §4.3, D77, D78; requirement FR-NET-2; [OpenShell review](../reviews/openshell-2026-11.md)

## Context
The handoff of 2026-11-05 listed both as "not planned" and recommended leaving them out. The owner
decided to build them.

- **OpenShell.** srt (ADR-0013) only covers traffic that goes through its proxy and can't tell which
  program connected. OpenShell's supervisor checks every TCP connection from a sandbox, for any
  protocol, and records each executable's hash. It needs Docker, Podman or KVM, is at 0.1.x, and
  its gateway sends anonymous telemetry by default. The review suggested it as an optional second
  backend for people who already run containers.
- **TLS inspection.** Egress sees only the host and port of third-party HTTPS (spec §3.2, T-17).
  Method and path rules ("GET on api.github.com/repos/**, no DELETE") need the request, so egress
  has to end the tool's TLS itself, with a certificate the tool trusts.

## Options
1. **Leave both out** (the earlier recommendation). Nothing new to break. srt's gaps stay, and
   rules stay per host.
2. **OpenShell as a full replacement for srt.** Stronger boundary for everyone, but every user
   would then need Docker, Podman or KVM, plus an image per agent. Rejected: the reference machine
   runs without containers.
3. **OpenShell as an opt-in backend per environment, Cairnward still the policy authority**
   (chosen). The OpenShell policy allows only Cairnward's gateway and egress ports. Model and tool
   calls, approvals and the audit stay Cairnward's.
4. **TLS inspection inside OpenShell only** (its supervisor already terminates TLS). Rejected as the
   only route: it would leave srt runs, MCP servers and helper programs without it, and put the
   rules in OpenShell's policy, outside Cairnward's audit.
5. **TLS inspection in cairnward-egress, per environment, off by default** (chosen), with a local
   CA, method and path rules, and one audit record per request.

## Decision
**D77, OpenShell backend.** `sandbox = "openshell"` in an environment (isolation on) starts
`cairnward run` agents in an OpenShell sandbox.
- **Policy:** two `protocol: tcp` endpoints, `host.openshell.internal` on the gateway port and on the
  egress port, for every binary (`/**`). Nothing else is reachable, whatever a program does with
  proxy settings. Egress still applies the environment's network setting, approvals and audit.
- **Files:** the sandbox has its own file system, so the workspace is copied in, and copied back
  when the run ends: new and changed files are written, deleted ones deleted. Nothing is written
  or deleted through a symlinked folder, and a folder is never replaced by a symlink, so the copy
  back can't reach outside the workspace. Such paths are listed and the sandbox kept. If the copy
  back fails, the workspace is left alone and the sandbox kept. Snapshots still work.
- **The run key** goes in an env file uploaded into the sandbox and sourced before the agent
  starts. It never appears in a command line on this machine (`sandbox exec` has no way to pass
  variables except as arguments).
- **Program pinning:** Cairnward doesn't pin the program, because it's in the image. OpenShell
  pins each executable itself.
- **Telemetry:** the run warns that OpenShell's gateway sends anonymous telemetry unless
  `OPENSHELL_TELEMETRY_ENABLED=false` is set for it. Cairnward doesn't run that gateway, so it
  can't switch telemetry off itself.

**D78, TLS inspection.** `[tls_inspection]` in an environment, `enabled = false` by default, never
set by a preset (spec §5.0 lists it as opt-in).
- **Interception:** when on, egress answers a run's CONNECT with a certificate from Cairnward's
  local CA, opens its own TLS connection to the host, verified against the public roots, and
  passes HTTP/1.1 requests through h11. Each request is checked against `rules` (host, method,
  path glob, first match wins) and `unmatched` (allow or deny). A denied request gets a 403 from
  egress and never reaches the host.
- **CA:** an EC P-256 key, valid for two years, in `<data_dir>/egress-ca/` (0600), which sandboxed
  agents can't read. Host certificates are made on demand, valid for seven days, and kept in
  memory only. `cairnward egress-ca show|rotate` manage it, and a rotation is audited.
- **Who is inspected:** only `cairnward run` agents in that environment. They get a bundle
  (public roots plus the CA) through `SSL_CERT_FILE` and similar variables, and their egress
  session is marked as given the CA. Other programs in the environment (MCP servers, restic,
  `mcp login`, plugin commands, downloads) are tunnelled unread. Amended 2026-11-05 after the
  threat-model review found that inspecting them broke their HTTPS.
- **Domain fronting:** a request naming another site than its tunnel is refused. Hosts in `skip_hosts` are tunnelled unread, for clients
  that pin certificates. The core's own connections are never inspected.
- **Audit:** one `net.request` record per request: method, status, bytes, the deciding rule, and
  a digest of the path and query, never the text. A plain-HTTP request to an inspecting
  environment follows the same rules.
- **Visible weakening:** turning it on adds a privacy warning (doctor, `config check`, serve, the
  dashboard) and a warning at the start of each run.

## Consequences
- **Two more ways a run can fail:** a tool that ignores `SSL_CERT_FILE` or pins certificates
  fails to connect, and the audit says why. An OpenShell gateway that isn't running fails the
  run before the agent starts.
- **The CA key is a new valuable secret:** anyone holding it can impersonate any site to tools
  that trust it. It's local, owner-only, outside every sandbox, and rotatable. A rotation doesn't
  reach running egress processes until restart.
- **Not inspected:** HTTP/2 is offered only as HTTP/1.1 (ALPN), and WebSocket messages after the
  upgrade aren't. Request bodies are passed on, not redacted.
- **Not tested for real:**
  - OpenShell was tested against a stand-in CLI, not a real gateway. Whether
    `host.openshell.internal` reaches Cairnward's loopback-only ports depends on the OpenShell
    driver's network mode. The Docker driver's default (supervisor in host networking) should,
    but it wasn't tried. Neither the `/**` binary glob nor the policy's acceptance was checked
    against a real gateway.
  - TLS inspection was tested with real TLS, httpx and curl against a local HTTPS origin, not
    against outside sites or agents.
