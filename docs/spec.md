# Spec: Cairnward, a local privacy gateway

Status: **Draft v0.8, for review**. Rewritten for clarity on 2026-11-10. The decisions did not change.

- **v0.8:** defaults and performance targets come from the reference-machine benchmark ([results](bench/results-2026-11-01-ryzen3-3200g.md)): 8K default context, 1.7B fast model, cache-aware scheduling (D59–D64).
- **v0.7:** every privacy and security feature is optional. Presets (Open, Balanced, Strict) set them, and each environment can override them (§5.0, D55–D58).
- **v0.6:** a change of direction after the [project critique](#13-history). Cairnward is a client-neutral privacy gateway that existing chat apps and coding agents plug into. Earlier versions specified an all-in-one Claude-app clone. Decisions that no longer apply are listed as superseded in §2.

Date: 2026-10-31. Repository: `c23075/cairnward`. License: **Apache-2.0**.
Name: **Cairnward**. Cairns mark a trail (the audit trail), and *ward* means to guard. The command is `cairnward`, with the short alias `cw`.

**Companion documents**
- [`requirements.md`](requirements.md): numbered requirements (FR-, NFR-) with priority and verification.
- [`threat-model.md`](threat-model.md): STRIDE and LINDDUN, mapped to the OWASP Top 10 for LLM Applications (2025).
- [`limitations.md`](limitations.md): what the protections do not do.
- [`adr/`](adr/): architecture decision records.
- [`plan/`](plan/): milestones, plans and the current handoff.
- [`bench/`](bench/): benchmark instructions and results.

---

## 1. Purpose

Local AI tools (chat apps, coding agents) run on your machine. Most of them give you no verifiable record of what they did. They also give you no enforced control over what leaves the machine. Cairnward sits between those tools and the outside world:

1. **Gateway.** One local endpoint for models (OpenAI-compatible) and tools (MCP). Cairnward checks every model call, tool call and outbound connection against a policy. It records each one in a tamper-evident audit log and shows it in an egress report.
2. **Enforced isolation.** Tools run in a sandbox. The only network route out of the sandbox is Cairnward's egress proxy. The operating system enforces this, not the tools.
3. **Transcript vault.** Cairnward keeps its own copy of every conversation that passes through it. You can search, lock, replicate and reliably delete that copy, whichever client you used.
4. **Plugins under a contract (D75).** Other programs run sandboxed with no network of their own. They ask Cairnward for models and outside requests. The first plugin is a separate literature-review project.

The main uses are **coding** (existing coding agents run through the gateway) and **research** (through a plugin, or any client).

Every protection is **optional** (§5.0). With all of them off, Cairnward is a plain model router with virtual models. You can switch each protection on globally or per environment.

### 1.1 Goals
- **G0. User choice.** You can switch every privacy and security feature on or off, globally or per environment, with a preset or one by one. Cairnward shows and logs every weakening.
- **G1.** Work fully offline with open-weight models (Qwen, DeepSeek) on a CPU-only machine with 32 GB RAM.
- **G2. Enforced choke point.** With isolation on, no tool, client or Cairnward component reaches the network except through the egress proxy. The operating system enforces this.
- **G3. Verifiable record.** An audit log with checkpoints anchored outside the machine.
- **G4. Client-neutral.** Any OpenAI-compatible chat client or coding agent works. A tested set is documented.
- **G5.** Anything beyond the gateway is a separate plugin (D75), under one contract for all plugins.
- **G6.** Pluggable model backends (local, LAN, hosted). Hosted backends are denied by default.

### 1.2 Non-goals
- A chat app or a coding agent. Use existing ones through the gateway.
- A multi-user or team server, remote access, or cloud hosting.
- Training or fine-tuning.
- Native Windows. Linux comes first, and Windows works through WSL2 (D74).

---

## 2. Decisions log

These are the current decisions. Superseded decisions are listed at the end, with what replaced them. Major decisions have an ADR.

| # | Topic | Decision | ADR |
|---|---|---|---|
| D42 | Product direction | A client-neutral privacy gateway: model endpoint, MCP proxy, egress proxy, policy, audit, vault and dashboard. Research is a plugin (D75). | [0009](adr/0009-gateway-pivot.md) |
| D43 | Use cases | Coding and research are equal uses. Coding uses existing agents (Qwen Code, opencode, Aider, Goose, any OpenAI-compatible agent) through the gateway. | 0009 |
| D44 | Clients | Client-neutral. Two to three chat clients are documented and tested. Cairnward builds only a **dashboard** (audit, egress, approvals, vault, settings). | 0009 |
| D45 | Egress enforcement | **Enforced by the operating system.** Sandboxed processes have no network except a bridged connection to the egress proxy. Cairnward's own components run the same way. Only the small `cairnward-egress` process has real network access. | [0010](adr/0010-os-level-egress-enforcement.md) |
| D46 | User-only actions | Agent tools that open a **confirmation dialog** in the dashboard or CLI. The dialog shows the action **and its provenance**: what content came before the request. | — |
| D47 | Chat data | A **transcript vault**. Cairnward stores every conversation that passes through it. You can lock, replicate and purge it. | [0011](adr/0011-transcript-vault.md) |
| D48 | Effort and environment selection | **Virtual models** (`cw/<environment>:<effort>`). Picking one in any client's model list selects the effort and the environment. | — |
| D49 | Audit anchoring | Cairnward commits checkpoint hashes, at intervals, to a **private git repository** that you own. | [0008](adr/0008-hash-chained-audit-log.md) (amended) |
| D50 | Backup purge | Purging a conversation also **rewrites the restic snapshots** to remove it, then prunes them. | [0007](adr/0007-sync-restic-backup-and-event-log-replication.md) (amended) |
| D51 | Name, license | **Cairnward** (`cairnward`, alias `cw`), **Apache-2.0**. | — |
| D52 | Platforms | **Linux (Debian family) first.** Windows through WSL2 (D74). CI also runs the unit tests on Windows. | — |
| D53 | Heavy dependencies | A lean core. Presidio and other heavy libraries run as **optional workers**. | — |
| D54 | Approvals | Approvals are tiered by risk and batched in an inbox, so that you see fewer prompts. | — |
| D55 | Optional protections | You can switch off **every** privacy and security feature: isolation, network blocking, approvals, redaction, vault, audit log, anchoring, crypto-shredding, backup purge and MCP pinning. | [0012](adr/0012-optional-privacy-controls-and-presets.md) |
| D56 | Defaults | You choose a **preset at first run** (`cairnward setup`): Open, Balanced or Strict. You can change it at any time. It shows as Custom when single settings differ. | 0012 |
| D57 | Granularity | **Global settings, with per-environment overrides.** A virtual model therefore also selects the privacy level. | 0012 |
| D58 | Weakening | Weakening a setting needs no extra step, but it is **visible** (dashboard badge, `cairnward status`, `cairnward privacy show`) and **logged**. Every change gets an audit record, written before the audit log itself is switched off. | 0012 |
| D59 | Reference machine | Ryzen 3 3200G (4 cores), 29.3 GB usable RAM, no GPU, Debian 13. The main model reads prompts at about 20 tokens/s and writes at 2–11 tokens/s. The 1.7B model reads at about 50 tokens/s. | — |
| D60 | Default context | **8K** tokens. 16K is allowed with a warning. 32K only on machines with more RAM or speed. | — |
| D61 | Default models | Main: `qwen3:30b` (30B-A3B MoE). **Fast: `qwen3:1.7b`** (2.3 GB, loads in 2 s, reads 2.5 times faster). `qwen3:4b` was no faster than the 30B MoE on this CPU, so it is offered only as a low-memory main model. | — |
| D62 | Cache-aware gateway | The gateway never changes a conversation's prompt prefix. It keeps the main model loaded while it is in use. **Conversation affinity** lets follow-up turns reuse the cache (measured 18.6 times faster). It saves and restores caches per conversation where the backend supports it (llama.cpp slots). | — |
| D63 | Tiered research pipeline | Moved to the research plugin (ADR-0017). See the plugin's `docs/design.md`. | [0017](adr/0017-research-as-a-plugin.md) |
| D64 | Coding agents on this hardware | Lean agents first (**Aider**). Agents with large prompts (Qwen Code, opencode, Goose) work but are slow on a CPU: the first turn of a 10K-token prompt takes about 8 minutes. | — |
| D65 | MCP protocol layer (2026-11-02) | The proxy uses the **official MCP Python SDK** for the protocol, in both directions. The client endpoint is stateless. Cairnward's policy, approvals, pinning and audit do not change. Only standard fields reach clients: Cairnward drops other fields and audits that. Pins hash the canonical JSON that clients get. Old pins carry over only when nothing changed. | [0015](adr/0015-mcp-proxy.md) (amended) |
| D66 | Dashboard code (2026-11-02) | **Plain ES modules without a build step**, so no npm supply chain runs in the page where you answer approvals. Revisit only if the dashboard grows a lot. | [0016](adr/0016-dashboard-without-a-build-step.md) |
| D67 | Reference formatting | Moved to the research plugin (ADR-0017). | [0017](adr/0017-research-as-a-plugin.md) |
| D68 | Richer PDF parsing | Moved to the research plugin (ADR-0017). | [0017](adr/0017-research-as-a-plugin.md) |
| D69 | Third-party timestamps | An **RFC 3161 timestamp authority** can also timestamp audit checkpoints. This is opt-in. Only the checkpoint's hash leaves the machine. | 0008 |
| D70 | Hosted providers | OpenRouter, DeepSeek API, Alibaba Model Studio, Groq, Cerebras and Gemini. They are denied by default and never used for locked conversations. Backends that **may train on prompts** (most free tiers) are marked. By default they answer **only in environments marked scratch**. You can switch this restriction off, visibly and audited. | — |
| D71 | M3 scope and order (2026-11-02) | Hardening first (a test matrix for each server mode, fuzzing, a one-command eval and checklist runner, an OpenShell review). Then the MCP SDK, restic backups, hosted providers, Presidio and OpenTelemetry export. Research items moved to the plugin. Multi-device replication and multi-agent workflows were deferred, and both were built later. | [plan/m3.md](plan/m3.md) |
| D72 | Replication (2026-11-04, FR-SYNC-4) | Through a **shared folder** (Syncthing, NAS, synced drive). Each device writes one append-only, signed and encrypted log per conversation. Purging a conversation deletes each device's file for it, so the purge reaches the folder. Turns written at the same time on two devices become a fork in the conversation's message tree, not a second conversation. A new device needs approval. There is no git-remote or rclone transport. | [0007](adr/0007-sync-restic-backup-and-event-log-replication.md) (amended) |
| D73 | What agent runs can read (2026-11-04) | `home_access`. With `hidden` (the default in Balanced and Strict), a run sees only the parts of the home folder that it needs. A list of secret folders cannot cover documents and other projects. `readable` is the opt-out and shows as a weakening. A run key is also pinned to its environment, because the environment selects the privacy level. | — |
| D74 | Windows (2026-11-04, NFR-PORT-2) | The owner uses a Windows laptop, so WSL2 moved from Won't to Should. Cairnward runs inside WSL2 with the Linux sandbox. WSL1 is refused. Under WSL, every sandbox hides the Windows drives (found in the mount table), `/init` and `/run/WSL`, because a Windows program runs outside the sandbox. Cairnward's own folders may not be on a Windows drive. There is no native Windows isolation. | — |
| D75 | Research as a plugin (2026-11-05) | Research moved to its own repository and runs under a general plugin contract. Coding and research stay equal uses, but research is a consumer of the gateway. The workflow runner moved with research. Backups stay in the outer process. The dashboard has no job page: the plugin's tools and CLI report jobs. Plugins come only from a path that you name. | [0017](adr/0017-research-as-a-plugin.md) |
| D76 | Plugins bring their environments (2026-11-05) | The core ships only `coding` and `scratch`. `cairnward-research install-plugin` writes the `litreview-*` environments as ordinary files in `environments/`, and keeps any files that are already there. | [0017](adr/0017-research-as-a-plugin.md) |
| D77 | OpenShell as a second sandbox (2026-11-05) | Opt-in per environment (`sandbox = "openshell"`, isolation on). The OpenShell policy allows only Cairnward's gateway and egress ports, so Cairnward stays the policy authority. The agent works on a copy of the workspace that is mirrored back. Tested only against a stand-in CLI. | [0018](adr/0018-openshell-sandbox-and-tls-inspection.md) |
| D78 | TLS inspection in egress (2026-11-05) | FR-NET-2. `[tls_inspection]` per environment, off by default, never set by a preset. A local CA, method and path rules, and one audit record per request (the path as a digest). Cairnward never inspects the core's own connections. | [0018](adr/0018-openshell-sandbox-and-tls-inspection.md) |
| D79 | Merkle checkpoints (2026-11-10) | Each anchored checkpoint also signs the RFC 9162 Merkle root over records 1 to seq. `audit verify --anchors` recomputes each root and checks that each checkpoint extends the one before. `audit prove` and `audit check-proof` show that one record is in a signed checkpoint, without the rest of the log. | [0008](adr/0008-hash-chained-audit-log.md) (amended) |
| D80 | Database encryption (2026-11-10) | Optional SQLCipher for `vault.db`, `keys.db` and `audit.db`, off by default. A passphrase, asked at start, wraps the key. A `[storage] key` reference is an optional, visible weakening for hosts that start unattended. | [0019](adr/0019-sqlcipher-encrypt-everything.md) |
| D81 | Rust audit verifier, not writer (2026-11-10) | ADR-0020 stage 4 builds an independent verifier (`cairnward-audit-rs`), not a Rust writer. With D80, a Rust writer would need a second process that holds the database key. The verifier recomputes the hashes, the chain and the Merkle roots from an export. | [0020](adr/0020-rust-egress-and-audit.md) |
| D82 | Read-only views for an observer (2026-11-11) | The management socket gains `GET /v1/runs` (active runs, without keys or tokens) and `GET /v1/audit/since?seq=&limit=` (records after a sequence number, as `audit log --json` writes them). They change nothing and aren't audited. [Cairnward Keep](https://github.com/c23075/cairnward-keep), the fleet control plane, reads them through an agent on each host and never writes to the socket. | — |
| D5, D19 | Model backends | Pluggable: Ollama (default), llama.cpp, or any OpenAI-compatible server. Set per role, environment or effort level. | [0004](adr/0004-pluggable-backends.md) |
| D29 | Hosted providers | Denied by default. Tested first: Alibaba Model Studio, DeepSeek API, OpenRouter. Extended by D70. | 0004 |
| D7, D25 | Literature sources, citations | Moved to the research plugin (ADR-0017). | [0017](adr/0017-research-as-a-plugin.md) |
| D9 | Stack | Python. The egress proxy and the audit log were the first candidates for Rust. An opt-in Rust egress data path and a Rust audit verifier now exist (ADR-0020). | [0003](adr/0003-python-now-rust-later.md), [0020](adr/0020-rust-egress-and-audit.md) |
| D11, D21 | Hardware | 32 GB RAM, CPU only. The default context is set by D60 (8K), and you can change it. | — |
| D12, D20 | Effort levels | Three default levels, plus levels that you define in `effort.toml`. | — |
| D13 | Environments | Profile bundles: workspace, tools, network policy, sandbox, models, privacy. | — |
| D15 | Skills and tools | `SKILL.md` folders, and MCP servers with permission manifests, served through the MCP proxy. | — |
| D22 | Artifacts | One git repository per conversation, plugin job and agent run. | — |
| D23, D24, D31, D35, D36 | Encryption | Every conversation has its own key (age format). Passphrase locks are opt-in and per conversation, with key wrapping. Purging destroys the key (crypto-shredding). | [0006](adr/0006-encryption-age-key-wrapping-crypto-shredding.md) |
| D27, D30, D40 | Sync | Modes `none`, `on_demand` and `auto`. restic backups, per-device replication logs, git for artifacts. | 0007 |
| D32–D34, D37–D39, D41 | Practice | ADRs, STRIDE and LINDDUN, local API hardening, OpenTelemetry export, MCP pinning, WCAG 2.2 AA for the dashboard, contracts and supply chain. | 0001, [0005](adr/0005-local-core-service-and-api-hardening.md), 0008 |

**Superseded in v0.6:**
- D1 (hybrid all-in-one build) → D42.
- D2 (TUI and web chat clients) → D44 (dashboard only).
- D6 (own coding flow) → D43.
- D10 (multi-agent everywhere) → research workflows only, now in the plugin.
- D16, D17 (Claude-app feature set, "thin slice of everything" MVP) → D42 and the milestones in §9.
- D28 (Windows sandbox tiers) → D52, then D74.
- D26 (name "Cairn") → D51.
- Chat folders and shortcuts in an own client → the vault browser (§4.5).

---

## 3. Architecture

This is isolated mode (isolation on), as built.

```
 Chat clients (Open WebUI, Jan, LibreChat, ...)          Coding agents (Aider, opencode, Qwen Code, Goose)
 optionally in a container whose only way out is         started with `cairnward run -- <agent>` in an
 Cairnward's client socket (§4.4)                        srt or OpenShell sandbox (§3.2, D77)
            │ OpenAI API / MCP, client key                         │ run key; network only to gateway + egress
 ┌──────────▼──────────────────────────────────────────────────────▼──────────────────────────┐
 │ OUTER PROCESS: the only process with network access                                         │
 │   forwarders: 127.0.0.1:8484 → gateway, :8485 → dashboard, client socket → gateway          │
 │   cairnward-egress :8486: a session per client and run, policy per host, address checks,    │
 │     optional TLS inspection (D78), an audit record per connection                           │
 │   control API (Unix socket): egress sessions, starting sandboxed MCP servers and plugins,   │
 │     backups, settings edits                                                                 │
 │   background: backups (restic), audit anchors, replication, OpenTelemetry export            │
 │  ┌──────────────────────────────────────────────────────────────────────────────────────┐  │
 │  │ CORE (srt sandbox: no network device; its own seccomp filter refuses VM sockets)      │  │
 │  │   model endpoint /v1 (virtual models)          MCP endpoint /mcp (pinned tools)       │  │
 │  │                 └───────────────┬──────────────────┘                                  │  │
 │  │                          POLICY ENGINE ──► approvals inbox ──► dashboard / CLI        │  │
 │  │     AUDIT LOG     TRANSCRIPT VAULT    REDACTION      PLUGIN SERVICES   ARTIFACTS      │  │
 │  │    (hash chain)   (age key per       (patterns,     (model, fetch,    (git repos)     │  │
 │  │                    conversation)      Presidio)      storage, keys)                   │  │
 │  │   management API (cairnward.sock), gateway and dashboard on Unix sockets              │  │
 │  └──────────────────────────────────────────────────────────────────────────────────────┘  │
 └─────────────┬────────────────────────────────────────────────────────┬─────────────────────┘
               │ model calls (local servers are                         │ everything else, through egress
               │ allow-listed in the core's sandbox)                    │
       ┌───────▼───────────┐                              ┌─────────────▼──────────────────────────┐
       │ Local models       │                              │ allowed hosts only: hosted models      │
       │ Ollama, llama.cpp  │                              │ (HTTPS), remote MCP servers, sites     │
       └────────────────────┘                              │ that agents and plugins may reach      │
                                                           └────────────────────────────────────────┘
```

Each MCP server and plugin runs in its own srt sandbox. The outer process starts it (srt cannot nest) and connects it to the core over a socket. With isolation off, the outer process and the core are one process. Agents and MCP servers in an environment with isolation on are still sandboxed.

The diagram shows the **Strict** configuration. You can switch off each box below the policy engine, and the sandboxing itself (§5.0). The endpoints, routing and virtual models always work. [limitations.md](limitations.md) says what none of this protects against. [architecture-uml.md](architecture-uml.md) has UML diagrams of the processes, the main classes and two request flows.

**Rules** (when the related protections are on)
1. Clients and agents talk only to Cairnward (model endpoint, MCP proxy) and, if allowed, to the egress proxy. With isolation on, the operating system enforces this (§3.2). With isolation off, the proxy settings are advisory, and tools can connect directly.
2. Every effect (model call, tool call, outbound connection, file write by Cairnward) goes through `policy.check → allow | ask | deny`. With audit on, it gets an audit record before and after. A protection that is off makes the policy engine return `allow` for its category, without asking.
3. `cairnward-egress` is the only process with real network access. It allows hosts and logs connections.
4. Cairnward reaches the local model server through a Unix socket or a loopback port bridged into the sandbox. The model server needs no internet access after you download the models.

### 3.1 Local API and dashboard security
See [ADR-0005](adr/0005-local-core-service-and-api-hardening.md).
- The CLI uses a Unix socket.
- The dashboard listens on loopback only. It checks the `Host` header against an allowlist and checks `Origin`. It uses CSRF tokens, `SameSite=Strict` cookies, a strict CSP and single-use sign-in links. It sends no CORS headers.
- The model endpoint and the MCP proxy take a separate **API key** per client (`cairnward client add open-webui`). The audit log therefore knows which client made each request, and each client can have its own policy.

### 3.2 Egress enforcement by the operating system (D45, [ADR-0010](adr/0010-os-level-egress-enforcement.md))

**Mechanism.** Anthropic's open-source **sandbox-runtime** (`srt`, pinned) wraps bubblewrap, seccomp and filtering proxies. `cairnward-egress` is its parent proxy ([ADR-0013](adr/0013-reuse-sandbox-runtime-for-isolation.md), spike results in `spikes/t06-sandbox/`). Isolation needs Node.js 22.12 or later.

**What each kind of program gets**
- **Agents and CLI tools** (`cairnward run -- qwen`) run under bubblewrap in a **new network namespace that has only a loopback interface**. Unix sockets, bridged to loopback ports, connect them to Cairnward's model endpoint, MCP proxy and HTTP(S) proxy. `HTTP_PROXY` and `HTTPS_PROXY` point at the bridge. A program that ignores the proxy settings has no route out.
- **Files for agents:** the workspace and the agent's own configuration folder are read-write. The home folder is hidden except what the run needs (`home_access`, §4.3). The backup and replication folders are always hidden. The rest of the system is read-only.
- **Server-style clients** (Open WebUI, LibreChat) run in **Podman or Docker containers on an internal network**. The only other member of that network is Cairnward. Compose templates are provided.
- **Cairnward's core** runs in the same kind of namespace, so its own libraries (for example Presidio downloading a model) cannot bypass the proxy. Model and data downloads go through `cairnward-egress` and are audited.
- **`cairnward doctor`** tries direct connections from inside each sandbox and expects them to fail. It does this independently of srt.
- **Programs outside the sandbox** can still reach local model servers directly (for example Ollama on 127.0.0.1:11434) and bypass Cairnward. The client guides set chat apps to use the gateway and to switch off their own Ollama connection.

**What the proxy can see.** Host names (from CONNECT and SNI), bytes and timing, for all connections. It sees **content** only for traffic that Cairnward starts or ends itself: model calls, MCP tool calls and plugin fetches. **Content redaction applies to that traffic only.** For third-party tools' own HTTPS, the policy works per host (allow, ask or deny), unless the environment turns on TLS inspection (below).

**`cairnward-egress`** (built in T0.9) is an asyncio HTTP proxy on loopback.
- **Sessions.** Each connection carries a session token in its proxy credentials (`http://cw:<token>@127.0.0.1:8486`). The token identifies the environment, the client and the run. No token, or an unknown one, gets `407`. Egress never forwards the token.
- **Policy.** Every `CONNECT host:port` and every plain-HTTP request goes through the policy engine. An approval that nobody answers within `[egress] approval_timeout_s` becomes a deny. A denied request gets `403`, `X-Cairnward-Decision: deny` and the reason. An allowed one is tunnelled and audited with byte counts and duration.
- **Addresses.** Egress resolves each name once and dials the address that it checked. It refuses names that resolve to loopback, link-local, multicast or cloud-metadata addresses, including IPv4 hidden in IPv6 forms, unless that exact IP is allowed.
- **Plain HTTP.** Egress sets the `Host` header from the checked URL, never from the client. It passes one request per connection, through h11, and tells the client `Connection: close`. It never sends what the client wrote after that request, so a pipelined second request cannot reach the host unchecked. A malformed request, such as one with two `Host` headers, gets `400`.
- **Approvals** are asked one at a time. A prompt that is still open after its connection timed out holds back the next one. A late answer is dropped, not remembered.

**TLS inspection** (D78, [ADR-0018](adr/0018-openshell-sandbox-and-tls-inspection.md)). `[tls_inspection] enabled = true` in an environment. It is off by default, no preset sets it, and it shows as a privacy warning.
- **How it works.** Egress ends a run's CONNECT with a certificate from Cairnward's local CA (`<data_dir>/egress-ca/`, EC P-256, file mode 0600). Host certificates are valid for seven days and kept in memory only. Egress opens its own verified TLS connection to the host and passes HTTP/1.1 through h11. ALPN offers only `http/1.1`.
- **Rules.** Egress checks each request against `rules` (host, method, path glob, first match wins) and `unmatched`. The query is not matched.
- **The path that rules see** is the path as a server resolves it:
  - escapes of unreserved characters are decoded (`%2e` is a dot, `%61` is `a`)
  - repeated slashes become one
  - `.` and `..` segments are removed, never above the root.
  A path with an encoded slash, backslash or NUL (`%2f`, `%5c`, `%00`, `\`) is refused, whatever the rules say.
- **Every reading of the path.** Servers read paths differently. Egress checks each request under four readings: as written, without `;` parameters (`;` or `%3B` up to the next `/`, which Tomcat, Jetty and Spring drop, so `/a/..;/b` is `/b`), and both of those with letter case ignored (rule patterns too). The first matching rule decides each reading. The request goes out only if every reading is allowed, and a denial names the reading that caused it. So neither case nor parameters can take a request past a deny rule or out of an allow rule. The cost: a legitimate request is refused if its case or `;` parameters change which rule matches.
- **Denials and audit.** Egress forwards an allowed request as sent. A denied request gets `403` and `X-Cairnward-Decision: deny` and never reaches the host. Each request gets a `net.request` audit record: method, status, bytes, the rule, and a digest of the path and query, never the text. Plain-HTTP requests in an inspecting environment follow the same rules.
- **What is not inspected.** Hosts in `skip_hosts` are tunnelled unread, for clients that pin certificates. Egress never inspects the core's own sessions, or any program that was not given the CA: MCP servers, restic, `mcp login`, plugin commands and downloads. Only `cairnward run` marks its session as given the CA (`tls_trust`).
- **Domain fronting** (T-34). Egress refuses a request whose `Host` or absolute URL names a site other than the one that the tunnel was opened to.
- **The CA.** A run gets the CA bundle through `SSL_CERT_FILE`, `REQUESTS_CA_BUNDLE`, `CURL_CA_BUNDLE`, `GIT_SSL_CAINFO`, `PIP_CERT`, `DENO_CERT`, `AWS_CA_BUNDLE` and, for Node, `NODE_EXTRA_CA_CERTS`. `cairnward egress-ca show | rotate` manages the CA. A rotation is audited.
- **Limits.** Request bodies are passed on, not redacted. WebSocket messages after the upgrade are not inspected.

**Rust data path** (opt-in, [ADR-0020](adr/0020-rust-egress-and-audit.md)). `[egress] implementation = "rust"` runs `cairnward-egress-rs`, built from `rust/`.
- The Rust process handles connections, parsing, DNS, address checks and TLS inspection (rustls).
- It asks a decision service in the Python process (`egress/decisions.py`, a 0600 Unix socket) for sessions, policy, approvals, certificates, request verdicts and audit records. Each of these therefore has one implementation.
- The decision socket requires a secret on every connection. The binary gets the secret on its standard input. This matters because the isolated core can reach any Unix socket (T-39).
- Every egress, egress-fuzz and TLS-inspection test runs against both data paths. Both cost the same ([bench](bench/results-2026-11-10-egress-rust.md)), and the Rust path was reviewed ([review](reviews/rust-data-path-2026-11-10.md)).
- The default stays `python` until the Rust path has run for real (status A18).

**Isolated mode** (built in T0.13). srt gives the sandbox its own private loopback, so the core cannot listen on the host's `127.0.0.1:<gateway_port>` itself.
- The core (gateway, management API, vault, policy) runs inside srt. It serves **Unix sockets** in the state folder (folder 0700, sockets 0600).
- The egress process, the only process with real network access, forwards `127.0.0.1:<gateway_port>` byte for byte to the gateway socket.
- Inside the core, every connection goes through srt's proxy, including connections to local model servers. Local backend addresses are on srt's allow-list. Egress handles the rest.
- The core's own egress session is *trusted*. Egress logs and address-checks the core's model calls but does not decide them again: the core already checked them under the right environment.
- The core stops by itself if the egress process disappears.
- **The core's system-call filter.** The core's sandbox allows Unix sockets, so srt's seccomp helper is off there (it would also refuse VM sockets). The core installs its own filter at start. `socket(AF_VSOCK)`, `io_uring_setup` and foreign-ABI system calls fail with `EPERM`, and everything the core starts inherits the filter. Under WSL2 the core does not start without it. Elsewhere a missing filter is logged (I1, T-28).

**`cairnward run`** (built in T0.12). The agent gets a gateway key and an egress session for the run, from the management socket. Both are revoked at exit.
- The agent starts with a clean environment: no inherited tokens or cloud credentials, only what its profile needs. `no_proxy` and `NO_PROXY` are cleared, because srt sets them to loopback, and clients would then bypass the proxy and fail to reach the gateway.
- The srt sandbox lets the agent write only to the workspace and its state folders. The home folder is hidden except what the run needs (§4.3). These are always hidden: known secret folders, Cairnward's own configuration, data and state, and the backup and replication folders.
- The agent's network reaches only the gateway, and the hosts allowed for the environment through egress.
- srt refuses catch-all allow-lists. Cairnward therefore builds srt's allow-list from the effective policy (gateway address, allowed and approved hosts). It updates the list live through srt's `--control-fd` when you approve a host.
- `run.start`, `run.end` and every model call carry the run ID.

**OpenShell as a second sandbox** (D77, [ADR-0018](adr/0018-openshell-sandbox-and-tls-inspection.md)). With `sandbox = "openshell"` and isolation on, `cairnward run` creates an OpenShell sandbox.
- The image is `[openshell] image` (default: the gateway's). The policy allows only `host.openshell.internal` (`[openshell] host_name`) on the gateway and egress ports, as plain TCP, for every binary. The agent reaches Cairnward by that name, and egress applies the environment's network rules as for srt.
- The run's settings go in as an uploaded env file. The agent's shell reads it before the agent starts, so the run key never appears in a command line.
- The workspace is uploaded beside it. When the agent exits, Cairnward downloads the workspace and mirrors it back: it writes new and changed files and deletes deleted ones.
- Cairnward never writes or deletes through a symlinked folder, and never replaces a folder with a symlink. It lists such paths and keeps the sandbox. If the download fails, the workspace stays untouched and the sandbox is kept.
- Cairnward does not pin the program (it is in the image), because OpenShell pins executables itself. `home_access` does not apply: nothing from the home folder is in the sandbox.
- Tested against a stand-in CLI only.

---

## 4. Components

### 4.1 Model endpoint and backends
- **Endpoints:** `/v1/chat/completions` (streaming, tools), `/v1/embeddings`, `/v1/models`, `/health`. An Ollama-compatible endpoint (`/api/chat`, `/api/tags`) is a later option, for clients that speak only Ollama.
- **Backends:** named entries in `backends.toml`, with the `ollama` or `openai_compat` driver and `location = local | lan | hosted`. Hosted backends are denied by default, redacted, marked ☁, and never used for locked conversations. A fallback backend must be local.
- **Built** (T0.10, T0.11). `cairnward serve` runs the gateway and `cairnward-egress`. With isolation off they share one process. With isolation on, the core runs sandboxed and egress runs in a separate process (§3.2).
  - Clients authenticate with keys from `cairnward client add <name>`. Cairnward stores only a hash of each key.
  - A request with a non-loopback `Host` header gets `421`. No CORS headers are sent. `Origin` is not checked, so desktop apps that send one still work (I38).
  - The Ollama driver uses Ollama's native API, so it can set the context size, the thinking switch and the keep-alive time. The OpenAI-compatible driver sets thinking through `chat_template_kwargs.enable_thinking`.
  - Calls to LAN and hosted backends go through egress with a session per environment, so egress applies that environment's rules.
  - The API contract is committed as `api/openapi.json`.

**Virtual models** (D48). `/v1/models` lists one entry per environment and effort level, for example:

| Virtual model | Resolves to |
|---|---|
| `cw/coding:low` | fast model (`qwen3:1.7b`), no thinking, low limits |
| `cw/coding:high` | main model (`qwen3:30b`), thinking on, larger budgets |
| `cw/coding:medium` | main model, thinking on (it can't be turned off on this model) |
| `cw/raw/qwen3:30b` | pass-through to a specific model with the environment `default` |

A virtual model selects an effort level (thinking mode and budget, sampling, context size, token and time limits per request) and an environment (allowed tools, network policy, redaction, backend, privacy level). You define effort levels and environments in TOML (`effort.toml`, `environments/*.toml`).

**Model defaults** (D61). The benchmark is run again at each milestone to check the defaults against current releases. Default roles: main `qwen3:30b` (or its successor), fast `qwen3:1.7b`, a small Qwen-VL model for vision on demand, Qwen3-Embedding-0.6B for embeddings.

**Resolution and fallback** (built in T1.7). A virtual model resolves its backend and model in this order:
1. the request itself (`cw/raw/<model>`)
2. the effort level's own `model` or `backend`
3. the client (`backend`, `[clients.<name>.models]` in `clients.toml`)
4. the environment
5. the global roles.

A backend may name a `fallback` (and a `fallback_model`). Configuration validation refuses a fallback that is not local (FR-BACK-3). When the backend is unreachable, or answers 5xx before anything reached the client, Cairnward retries the request once on the fallback. It records this as `model.fallback` and in the call's audit record, and flags it in `X-Cairnward-Warning` (non-streaming only). Client errors (4xx) are not retried.

**Cache-aware scheduling** (D62). On a CPU, reading the prompt is most of the wait. Re-reading an unchanged conversation start is avoidable.
- The gateway **never changes the start of a prompt**: no injected timestamps, no reordering of the system prompt or tools. Anything that it must add goes at the end.
- **Conversation affinity.** Requests from the same conversation stay together. A request from another conversation waits briefly (configurable, default 0 s for interactive clients) rather than evict a warm cache.
- **Keep-alive.** The main model stays loaded while a session is active. It is unloaded after an idle period (default 10 minutes) to give the desktop its memory back.
- **Cache save and restore.** With the llama.cpp backend, each conversation's KV cache is saved to disk on eviction and restored when the conversation resumes (llama-server slot save and restore). A long conversation then resumes in seconds, not minutes. Ollama does not offer this, so the dashboard recommends llama.cpp for heavy use.
- **Memory check.** Before it loads a model, the gateway compares the model's size plus context with the free RAM. If loading would push the system into swap, it warns. In the Balanced and Strict presets it queues the request instead. `cairnward doctor` reports which models and context sizes fit.
- **Queues.** There is one queue per backend (`max_parallel`, default 1). The conversation whose prefix the model has cached goes first, at most three times in a row.
- **Waiting-time warning.** The gateway estimates each request's uncached prompt tokens from the previous request on that backend. With speeds imported by `cairnward bench import`, a request expected to need more than 60 s before the first token gets an `X-Cairnward-Warning` header and an audit note (about 1,300 new tokens on the reference machine's main model). A prompt larger than the context gets a warning, or is refused with `context_overflow = "reject"`.

**Context reporting.** Every response carries the standard `usage` counts, which most clients show. The dashboard's live view shows the context used against the limit, per conversation.

**Reasoning written into the answer** (T-26). Some models reason in their visible reply when thinking is off, and end with a stray `</think>`.
- The gateway moves that text to `reasoning_content`, audits the leak, and from then on asks that model for separate reasoning (`<state_dir>/reasoning_leaks.json`).
- A stream cannot be taken back. With thinking off, the first streamed reply of a model not yet seen to answer cleanly holds back its answer text. Tool calls, separate reasoning and finish signals still flow.
- When `</think>` arrives, the text before it goes out as `reasoning_content` and the rest streams live. A reply that ends without `</think>` is released whole before its finish, and the model is recorded as clean (`reasoning_clean.json`). It then streams directly (I5).

**Vault recording.** Each message gets a prefix hash over a normalised form (role, content, tool calls). A client that echoes the assistant's reply without `reasoning_content`, or with `null` content, therefore still continues the same conversation. An edited earlier message continues as a branch from that point.

**A run is pinned to its environment.** An environment also selects the privacy level (D57), so an agent must not be able to pick a more open one.
- A run key from `cairnward run --env X`, or a plugin's key, can make model calls only in `X`, as on the MCP routes.
- `/v1/chat/completions` and `/v1/embeddings` answer `403` (`run_environment`) to `cw/<other>:<effort>`. Cairnward audits this as a denied `model.call` under `X`, with `requested_environment` in the details. Nothing reaches a backend.
- `cw/raw/<model>` uses the run's environment, not the client's default.
- `cairnward run` refuses a `--model` from another environment before it starts the agent.
- A client's own key is not pinned and may name any environment (I39). `/v1/models` is not filtered: a run still sees every environment.

**Disabled clients.** A client with `enabled = false` in `clients.toml` gets `401` with any of its keys, its run keys included, on chat, embeddings, models and MCP. Its keys work again when you switch it back on. These refusals are not audited.

### 4.2 MCP proxy and tools
- **One MCP endpoint per environment** (`/mcp/<environment>`, streamable HTTP with JSON responses and per-client sessions), or `cairnward mcp` as a stdio bridge. Cairnward combines the MCP servers enabled for the environment and applies policy, audit and redaction to every call. The protocol layer is the official MCP Python SDK (D65).
- **Declared servers** (M2, [ADR-0015](adr/0015-mcp-proxy.md)). `mcp.toml` declares each server with a permission manifest:
  - hosts, reached through egress
  - writable folders, inside the workspace or the server's own folder
  - optional readable folders
  - variables passed through.
  It also sets `allow`, `ask` or `deny` per tool, and an optional `pin_file` with `sha256`. Environments list their `mcp_servers`.
- **Sandboxes.** Each server starts on first use, in its own sandbox, with only its declared permissions. With the core isolated, the outer process starts it (srt cannot nest) and gives the core a socket.
- **Calls.** Tools appear as `<server>__<tool>`. Each call is a `tool.call` action: policy, approvals, and pre and post audit records with digests of the arguments and the result. `cairnward run` passes the endpoint to agents (opencode's per-run configuration includes it).
- **Pinning.** Cairnward hashes each whole tool definition and offers only approved ones (`cairnward mcp tools | approve`). A changed definition is audited (`mcp.tool_changed`) and disables the tool until you approve it again. Descriptions are scanned for instruction-like text.
- **Built-in tools.** Opt-in per environment:
  - `tools = ["artifacts"]`: `cairnward__artifact_write | read | list` (§4.7)
  - `tools = ["vault"]`: `cairnward__lock_conversation`, a user-only action (§5.3).
- **Skills** (T2.10). Folders `<config>/skills/<name>/SKILL.md`, with front matter `name` and `description`. An environment offers the skills in its `skills = [...]` as MCP prompts (`prompts/list`, `prompts/get`, audited as `skill.use`). The skill's other files are read-only resources (`skill://<name>/<path>`). Hidden files and links that point out of the folder are not served. Scripts are never run.
- **Remote MCP servers.** `url` in `mcp.toml`: streamable HTTP through egress. See the ADR-0015 amendment.
- **OAuth for remote servers.** `auth = "oauth"` in `mcp.toml`.
  - `cairnward mcp login <server>` signs in once, in a terminal, with the MCP SDK's client: discovery, dynamic registration and PKCE. The answer comes back to a one-time listener on 127.0.0.1. The login's requests go through an egress session in the environment.
  - Tokens are kept owner-only in `<state>/mcp-oauth/<server>.json`, never in configuration or the audit log. `mcp logout` forgets them.
  - The core only uses and refreshes tokens. It never starts a sign-in. When a token cannot be refreshed, the connection fails with a message to log in again.
  - Policy checks every host on every request, the sign-in server's included.
  - Tested with the real SDK against a fake authorization server on real sockets. No real provider was tried yet.
- **Not built yet:** idle shutdown of servers, redaction of tool results.

### 4.3 Coding agents through the gateway (D43)
`cairnward run [--env coding] -- <agent> [args]` starts an agent in the sandbox, with its model base URL, API key and MCP configuration pointed at Cairnward. There are profiles for **Qwen Code, opencode, Aider and Goose**. A generic profile works for any OpenAI-compatible agent.

The agent keeps its own approvals for edits and commands. Cairnward adds:
- sandboxing of files and network
- an audit record of every model and tool call, linked to the run
- **workspace snapshots** before and after each run, so that you can review and undo every change
- budgets per run (tokens, time, requests), enforced at the model endpoint.

**What a run can read** (`home_access`, §5.0). With `hidden`, the run's sandbox denies the home folder and allows again only:
- the workspace and the run folder
- the profile's state folders
- git configuration (`~/.gitconfig`, `~/.config/git`) and the profile's settings files
- language runtimes (uv's Pythons, fnm, nvm, pyenv versions, mise, asdf, Volta, rustup)
- srt's package
- the program itself, found by following links and `#!` interpreters from the command, plus the active venv, the script that an interpreter runs, and folders on `PATH`.

Details:
- A program's whole prefix is allowed again only when it has `lib/` or `pyvenv.cfg`, never the home folder or `~/.local` as a whole.
- Known secret folders and Cairnward's own folders stay hidden inside all of that.
- Writes elsewhere under the hidden home folder go to a throwaway tmpfs and never reach the disk.
- If the workspace contains the home folder, hiding cannot apply. The run warns and uses the list of secret folders instead.
- `home_access = "readable"` (per environment) also uses that list, with a warning at each run. It shows as weaker than Strict, and a change is audited.
- A program that this search cannot follow (pyenv shims, an install that loads files outside its own folder) does not start. Use `readable` for that environment.
- `~/.env` is not allowed again for Aider.
- Tested in the real sandbox with a fake home folder that holds planted secrets, and against the real home folder. Tested with real Aider on an isolated server and a scripted model. Qwen Code's `npm install -g` layout was tested with a stand-in only.

**Snapshots.** Before and after every run, Cairnward commits the workspace into a shadow git store in the state folder, one per workspace.
- The project's own `.git` is never touched. `.gitignore` is honoured. Dependency and build folders, and files over 20 MB, are left out.
- Commands: `cairnward runs list | diff <id> | revert <id> [--force] | prune`.
- `revert` skips files changed again since the run, unless forced. It takes a safety snapshot first and is audited (`run.revert`).
- Snapshots are kept per workspace, not per run, so they cannot be locked.

**Profiles, tested with the real agents** in the sandbox under Strict, against a scripted model:
- Aider 0.86.2 (diff edit format by default, analytics off)
- Qwen Code 0.26.5
- opencode 1.19.5 (a per-run configuration file, removed with the run)
- Goose: the profile is not tested.

The sandbox blocked these calls, and all three tested agents kept working without them: Qwen Code's usage telemetry (`*.rum.aliyuncs.com`) and model catalogue (`models.dev`), opencode's provider download (`registry.npmjs.org`), and the price-list download of LiteLLM inside Aider (`raw.githubusercontent.com`).

**Budgets.**
- Per run: `--max-requests`, `--max-tokens` and `--max-minutes`, or `[run_budget]` per environment.
- Per client and day: `[clients.<name>.budget]` in `clients.toml`, with `max_requests_per_day` and `max_tokens_per_day`. Unset means unlimited. Embeddings count as requests only, and a client's runs count towards its limit.
- Going over a budget returns `429 budget_exceeded` and is audited.
- The daily counts are kept in `<state_dir>/client_usage.json`, so a restart does not reset them. The day is the machine's local calendar day.
- Limits are checked before a request. The request that crosses a token limit therefore finishes, and requests running at the same time can pass a limit slightly.

**Prompt sizes** (FR-AGT-1, T2.9). At the end of each run, the size of the first model call's prompt is recorded per profile (`<state>/prompt_sizes.json`). Before starting, `cairnward run` warns when the last size is larger than the virtual model's context, or, with an imported benchmark, would take more than 60 s before the first answer.

**Other tools.**
- `evals/coding/run_eval.py` runs an agent on a task repository through `cairnward run`. It reports pass or fail, time, model calls and tokens from the audit log.
- `CAIRNWARD_SANDBOX_DEBUG=1` makes the sandbox log every allowed and blocked connection, to show what an agent tries to reach.

**Approvals during runs.** Every "ask", from the gateway, egress or a sandboxed run, waits in the core's approvals inbox.
- You answer in the dashboard, or with `cairnward approvals` (watch mode with y, c, e and n, `list [--json]`, or `answer <id> <reply>`). A desktop notification goes out through `notify-send` when it is available.
- No answer within `approval_timeout_s` means no.
- In isolated mode, the egress process asks the core's inbox over the management socket.
- With `network = "ask"` (or `"open"`), `cairnward run` starts srt through a small Node wrapper around srt's library (`sandbox/srt_ask.mjs`). Its ask callback sends hosts that are not on the sandbox's list to the core. The policy engine decides for the run's environment. An approved host stays allowed for the rest of the run, in the core and in egress, and every grant is audited (`approval`).
- srt connects to loopback destinations directly, not through egress. The core therefore applies egress's address checks when the sandbox asks. It refuses this machine's addresses (including `localhost` and shorthand such as `127.1`) and link-local and metadata addresses, unless they are listed exactly.
- This needs srt installed as the Node package (`cairnward sandbox install`). Without it, hosts not on the list stay blocked, with a warning.

### 4.4 Chat clients (D44)
- **Tested clients:** **Open WebUI** (check its current license's branding terms), **Jan** and **LibreChat** (T2.9: server-style, MIT, custom endpoints and MCP).
- **Setup guides** in [`clients/`](clients/) give the base URL, the per-client API key, the MCP endpoint, and recommended privacy settings: telemetry off, and client history minimal or temporary, because the vault holds the authoritative copy.
- Chat folders, uploads and artifact panels come from the client. The virtual model selects the effort and the environment (§4.1).
- **Containers.** Templates for Open WebUI and LibreChat put the client on an internal-only network.
  - Its only way out is Cairnward's **client socket** (`<state>/clients/gateway.sock`). `serve` creates the socket in both modes, owner-only, in its own folder.
  - A socat sidecar joins the socket to `127.0.0.1:8484` inside the client's network namespace.
  - The UI is published on 127.0.0.1 through a relay. Image versions must be pinned in `.env`.
  - Checked with `docker compose config`. The hands-on checks with the real apps are in [`clients/checklist.md`](clients/checklist.md) (owner-side).

### 4.5 Transcript vault (D47, [ADR-0011](adr/0011-transcript-vault.md))
- **Recording.** Cairnward stores every request and response through the model endpoint. It rebuilds conversations from the OpenAI API's stateless requests by **prefix hashing**. Each request carries the full history, so a request whose message prefix matches a stored conversation extends it. A different prefix starts a new conversation or a branch.
- **Encryption.** Each conversation has its own age key, which makes deletion reliable (crypto-shredding, §5.5). You can **lock** a conversation with a passphrase (§5.5).
- **Recovery key** (FR-ENC-1).
  - `cairnward vault recovery-key create` makes an age X25519 identity. It shows the secret once and stores only the public half (in `keys.db`).
  - From then on, each lock also encrypts the conversation key to it. Conversations locked earlier are added the next time they are unlocked, because the key is in memory then.
  - `cairnward vault recover <id>` takes the recovery key and sets a new passphrase. The conversation stays locked, and the recovery is audited (`vault.recover`).
  - `recovery-key status` says how many locked conversations the key covers. `recovery-key remove` destroys every copy.
  - The secret never reaches the audit log, the database files or the server (tested). Anyone with the secret and the data folder can open the covered conversations, so keep it off the machine.
- **CLI** (T2.4): `cairnward vault list | search | show | export | delete | restore | purge | pin | title | prune`, with ID prefixes.
  - Search decrypts as it goes. There is no plaintext full-text index. This is fast enough for a personal vault. Revisit if it gets slow.
  - Exports are plain-text files (mode 0600). Exports, deletes, restores and purges are audited.
- **Retention.** Per environment: keep forever (default), or `vault_retention_days` days. Retention purges an environment's conversations untouched for that many days, and keeps pinned ones. The trash empties after 30 days. Both run at start, daily in the core, and on `vault prune`, recorded as `vault.retention`.
- **No vault.** With `vault = "digests"` or `"off"`, Cairnward stores nothing in the vault. The audit log keeps digests.
- **The dashboard's vault view** (§4.9): search, folders, pin, lock, export, delete with undo, and keyboard shortcuts.
- **Client copies.** Deleting in the vault does **not** delete the copy in the client's own history. The dashboard says so, and the setup guides show how to keep client history small.

### 4.6 Plugins (D75, [ADR-0017](adr/0017-research-as-a-plugin.md), [guide](plugins.md))
A plugin is an MCP server with `kind = "plugin"`. It gets no capability that a protection depends on. Instead, it asks a core service. The service enforces the protection, and the sandbox stops the plugin from going around the services.

- **Sandbox required,** whatever the environment's `isolation`. `read` is required, and `write` only inside `{data}`. `unsandboxed = true` is the one way out. It is warned about, shown on the dashboard and audited.
- **No network of its own.** Only the gateway. `permissions.network` may list exact local addresses, such as Zotero's `127.0.0.1:23119`. They are shown at install and audited. An outside host is not allowed there, which is narrower than the ADR.
- **Services.** Each launch gets a key that is held in memory, revoked at stop and pinned to the environment. With it, the plugin can use:
  - the gateway, for models
  - `POST /v1/cairnward/fetch`, for outside requests. This applies a `net.connect` check and redaction, goes through egress and is audited as a digest. Hosts are limited to `fetch_hosts`. Limits: 2 MB out, 64 MB back, a rate limit, four requests at a time. Redirects are returned, not followed
  - `/v1/cairnward/artifacts`, which reaches only `plugin/<name>`
  - `/v1/cairnward/item-keys`, for a key stored with the conversations' keys
  - `/v1/cairnward/model-info`.
- **Pinned code and tools.** `pin_dir` must match `sha256`, or the plugin does not start. Changed tool definitions are held for review. Only `unpinned_tools` switches that off.
- **Output is untrusted.** After a plugin's output, and before your next message, a call to a tool on another server, or to Cairnward's own artifact tools, needs approval. The prompt names the plugin, and the approval is never remembered. Not covered: an agent's own network requests after reading plugin output, which egress cannot link to it.
- **Conformance.** `cairnward plugin verify <name>` starts a probe with the plugin's exact sandbox and key against a running core, then the plugin itself. A plugin that has not passed for its current code and contract version does not start.
- **No MCP for plugins.** A plugin's key cannot use the MCP endpoint, so a plugin cannot call other plugins' tools.
- **Versioned.** `contract_min` and `contract_max` must include the core's contract version (now 1).
- **Commands:** `cairnward plugin add | list | pin | verify | run | allow-read | helper | remove`. `plugin run` starts a command in the plugin's sandbox, with your terminal. `plugin helper` starts a program that cannot use fetch (for example rclone) outside the sandbox, with its traffic through egress.

Literature research is one such plugin: a separate project (`literature-review-agent-plugin`, package `cairnward-research`) with its own design, requirements, threat model, status and evals. The core knows nothing about it, except two kinds of leftovers that keep an older setup working:
- commands that moved print where they went
- old settings (`[research]`, `research_sources`, `include_research`, `citation_style`) still load. All except `include_research` are ignored.

A plugin brings its own environments if it needs them (D76). The core ships only `coding` and `scratch`.

### 4.7 Artifacts
One git repository per conversation, plugin job and agent run: one commit per write. Restoring writes the old content as a new commit.
- **Storage** (T1.9). Repositories are in `<data>/artifacts/<conversation|job|run>/<id>/`, owner-only. A write that changes nothing makes no commit. Paths stay inside the repository (no `..`, absolute paths or `.git`). Files are limited to 2 MB.
- **Purging** a conversation deletes its artifacts.
- **Access.** Models write through MCP tools (`cairnward__artifact_write | read | list`, opt-in with `tools = ["artifacts"]`). The tools are policy-checked and audited, and reach only the client's current conversation. Commands: `cairnward artifacts list | files | log | show | restore`, and the dashboard's Artifacts view (versions, restore). Agent runs use workspace snapshots instead (§4.3).
- **Previews** (FR-ART-4). HTML and SVG open in an `<iframe sandbox>` served with `Content-Security-Policy: sandbox; default-src 'none'; style-src 'unsafe-inline'; img-src data:`. That gives an opaque origin, with no scripts and no network. A browser test checks that a preview's script does not run and its external image is not fetched.
- **Locking** (FR-ENC-1). Locking a conversation seals its whole artifact repository, history and file names included, into one file (`sealed.age`). The conversation's own age key encrypts it, so its passphrase, recovery key and crypto-shredding all apply.
  - While the conversation is locked, nothing can read or write its artifacts. `cairnward artifacts` and the dashboard show it as locked.
  - While it is unlocked in the server, each operation works on a temporary copy, in `$XDG_RUNTIME_DIR` when there is one, else in the system's temporary folder. The copy is deleted afterwards, and a write seals the repository again.
  - Removing the lock restores the plain repository.
  - A repository under a conversation that was locked before this feature existed is closed at once and sealed at its next unlock.
  - Earlier plaintext is not erased from the disk's free space or from old backups. `purge` removes it from backups.
- **Jobs and runs.** `cairnward artifacts lock job|run <id>` locks a finished job (its artifact repository and its folder, leaving a stub with only its ID, kind, status and times) or a run's artifact repository. A new key, under its own passphrase and covered by the recovery key, encrypts it. `unlock` removes the lock and restores the files.

### 4.8 Sync and backup (D27, D30, D40, D50, D72, [ADR-0007](adr/0007-sync-restic-backup-and-event-log-replication.md))
Backup and replication cover the **vault, plugins' data and artifacts**. Each has the modes `none` (default), `on_demand` and `auto`.

**Backups** (T3.3). restic always encrypts.
- **Settings.** `[backup] mode`, `repository`, `interval_hours`, `keep_daily`, `keep_weekly`, `keep_monthly`, and an optional `password_command`. The default password is generated into the state folder and shown once at `cairnward backup init`.
- **Where backups go.** A local folder (a second disk, NAS, Syncthing folder, or a cloud drive mounted locally), or a remote restic backend that speaks HTTP (`s3:`, `rest:`, `b2:`, `azure:`, `gs:`, `swift:`, `rclone:`). `sftp:` is refused, because SSH would not go through egress. With no `repository`, backups go to `~/cairnward-backups`.
- **Setup.** `cairnward backup use [folder] [--mode] [--access owner|group]` switches backups on and sets up the repository.
- **Access.** `[backup] access` sets who can read a folder repository: owner-only by default. Cairnward applies it again after each backup, because restic creates new read-only files. A folder on the same disk as the data gets a warning in the CLI, `doctor`, `status` and the dashboard.
- **Remote repositories.** restic runs inside an egress session in `[backup] environment` (default: the default environment). That environment's network policy applies, and the traffic shows in the egress report under the client `restic`. `serve` must be running. The backend's credentials come from restic's own environment variables, never from Cairnward's configuration.
- **What a snapshot holds.** A **per-conversation export**: one folder per conversation, with its rows exactly as stored (ciphertext when encrypted) and its key (wrapped when locked). Also research.db, the audit log, artifacts, jobs, MCP pins and the configuration folder.
- **Staging** (I40). The export is built in memory (`$XDG_RUNTIME_DIR/cairnward-backup-staging`, a fixed name so that snapshots stay incremental). It is removed when the run ends, whether it failed or not. It goes on disk (`<state>/backup/staging`) only without a memory folder (with a warning), or with `[backup] staging = "disk"` for exports larger than memory.
- **Purges reach every snapshot** (D50). The vault queues each purge. The next run, or within the hour under `serve`, does `restic rewrite --forget --exclude conversations/<id> --exclude artifacts/conversation/<id>` and `restic prune`. It then checks with `restic find` that no snapshot still holds the conversation. Until then, the purge shows as pending (`cairnward backup status`, the doctor, the dashboard's vault view) and is retried.
- **Privacy of snapshots.** Snapshots record the host as "cairnward", not the machine name. For a folder repository, restic runs with no network settings.
- **Restore.** `cairnward backup restore [--at DATE | --snapshot ID]` brings back conversations that the vault does not have. It never overwrites, and purged conversations stay purged. `--to DIR` only extracts.
- **Isolated mode.** The outer process runs restic, because the sandboxed core cannot write to the backup folder.
- **From the dashboard.** The Backups view has **Back up now**, which sets up the repository the first time, and **Change settings** for the mode and a folder. A remote repository stays a change in `cairnward.toml`. Settings take effect on restart. Both actions are audited (`backup.requested`, `backup.settings`: which fields changed, not the path).
- **Tested:** purged content is absent from all snapshots afterwards. A restore works after losing the vault. A purge stays pending while the disk is unplugged.

**Replication** (FR-SYNC-4, FR-SYNC-6, D72).
- **Commands.** `cairnward replicate init <folder>` on the first device shows a sync key once. `replicate join <folder>` on another device asks for that key. `replicate approve <id>` on an existing device then accepts the new device's changes.
- **Settings.** `[replication] mode`. In `auto` mode, `serve` syncs every `interval_minutes` (in isolated mode, from the outer process). A restart picks up a change. `exclude_environments` keeps an environment's conversations on the device. `include_artifacts` adds artifacts.
- **What is shared:** conversations, their messages, keys and artifacts, and folders. Research libraries, jobs, settings and the audit log are not shared.
- **Format.** Each device writes only its own folder of files, so nothing can conflict at the file level.
- **Encryption.** The sync key encrypts every record (ChaCha20-Poly1305). The device signs it with Ed25519, and a device's ID is the hash of its public key. Each record is bound to its device, file and position. Message bodies stay encrypted with the conversation's key underneath. Encryption of the sync folder is therefore always on, and the sync key alone cannot read a locked conversation.
- **Merging.**
  - Messages are a set, ordered by (Lamport clock, device). Two replies to one message are a fork in the message tree, and `vault show` and the dashboard show it as a fork.
  - Conversation fields: the last writer wins, per field.
  - A purge always wins. Each device deletes its own files for that conversation when it next syncs.
  - A lock or unlock on one device follows on the others, with artifacts sealed or unsealed to match. The older copy of the key is deleted from the folder.
  - Artifacts are git bundles, merged the same way on every device. When both sides changed a file, one version stays at its path, and the other is kept beside it as `<path>.from-<commit>`. For a locked conversation the artifacts are one sealed file, and the later write wins.
- **Damaged or partial folders.** A missing folder (not mounted) is refused, and nothing is written. A file that got shorter, or a record that fails its checks, is reported, and reading that file stops there. A record that needs another file that has not yet arrived waits for it.
- **Blocking a device** stops Cairnward accepting its changes. The device keeps the sync key and can still read the folder. To shut it out, use a new folder: `leave`, then `init`, and `join` from the other devices.
- **Limits.**
  - A device that has not synced still holds a purged or newly locked conversation, and its files stay in the folder until it syncs.
  - The folder's own version history (Syncthing versioning, cloud drives) is outside Cairnward's reach. Switch it off for this folder.
  - Whoever holds the folder sees device and conversation counts, sizes and times.
  - A recovery key is per device.
- **Tested** in `tests/test_replication.py`, with separate device folders on one machine: random changes on three devices ending identical (Hypothesis), files delivered one by one in random order, tampering, and crashes. **Not tested between two machines, or through real Syncthing or a NAS.**

**Drive mirror.** An incoming, read-only mirror of a cloud drive through rclone, with preview.

### 4.9 Dashboard
A loopback web app, hardened as in §3.1. It is the only user interface that Cairnward builds. The terminal has equivalents for the main flows: `cairnward approvals`, `cairnward audit`, `cairnward vault`. The accessibility target is WCAG 2.2 AA.

- **Sign-in** (T2.7). `cairnward dashboard` asks the running core for a single-use sign-in link (valid for 2 minutes) and opens it. The link becomes an HttpOnly, SameSite=Strict session cookie and leaves the address bar.
- **Serving and security.** Served on `127.0.0.1:<dashboard_port>`. In isolated mode the core serves a Unix socket and the outer process forwards the port. Every change needs the CSRF token header and passes an Origin check. Strict CSP, no CORS, `no-store` on the API.
- **Views:**
  - approvals: live, with the provenance warning, a passphrase dialog for user-only actions, "yes to all like it", and late answers
  - activity: a live tail of the audit log
  - audit: filters, chain check, JSON-lines export
  - egress: filters, CSV
  - vault: search, trash, pin, folders, lock and unlock dialogs, export, delete with undo, and the shortcuts `j k Enter / d f L p x l ?` (you can switch them off in Settings)
  - MCP tools: permissions, and reviewing and approving definitions, with warnings
  - research jobs and artifacts
  - backups: mode, where (only a remote's host), its egress environment, retention, last backup, and purged conversations still in older backups, with retries and errors
  - settings: privacy per environment, with values weaker than Strict highlighted, backends, roles, clients, and the recovery key's status.
- **Editing privacy settings.** In Settings you can change the preset, and each setting with fixed values, globally or per environment. Allow-lists and backends are not editable there.
  - The change is written to the same TOML file that a person edits. Comments are kept. A built-in environment is first written out with its settings.
  - The change is saved only if the whole configuration still loads.
  - A change that leaves a setting weaker than Strict asks first: the server answers `409` until you confirm.
  - It is recorded as `settings.changed` by "user (dashboard)".
  - It takes effect on restart, and shows as "saved, not yet in effect" until then.
  - In isolated mode the core cannot write the configuration folder. It sends privacy and backup edits to the outer process over the control socket (`POST /v1/settings/{kind}`), which applies the same checks (T-32).
- **Tested** with request-level security tests and a Chromium end-to-end test: sign-in, live approval, vault shortcuts, an XSS probe, locking, no CSP violations, labelled controls, a skip link. The browser test also runs **axe-core 4.14.0** (WCAG 2.0, 2.1 and 2.2, A and AA, plus best practices) on all ten views, in light and dark mode, and requires zero violations. Dialogs and the sandboxed preview's contents are not checked.

---

## 5. Security and privacy

### 5.0 Privacy controls and presets (D55–D58, [ADR-0012](adr/0012-optional-privacy-controls-and-presets.md))
Every protection in this section, and in §3.2, §4.5 and §4.8, is a setting. A **preset** sets them all at once. You can then change single settings, and the preset shows as *Custom*.

**Settings and presets**

| Setting (`[privacy]` key) | Values | Open | Balanced | Strict |
|---|---|---|---|---|
| `isolation`: OS sandbox + network namespaces for agents, clients and the core (§3.2) | `on` / `off` | off | on | on |
| `network`: outbound access for tools and Cairnward (§3.2) | `open` / `ask` (new hosts need approval) / `allowlist` / `off` | open | ask | allowlist (empty by default = off) |
| `home_access`: what sandboxed coding agents can read in the home folder (§4.3) | `readable` (all but known secret folders) / `hidden` (only what the run needs) | readable | hidden | hidden |
| `hosted_backends` (§4.1) | `allow` / `ask` / `deny` | allow | ask | deny |
| `training_backends` (D70) | `allow` / `scratch_only`: backends marked `trains_on_prompts` (most free tiers) answer only in environments with `scratch = true` | allow | scratch_only | scratch_only |
| `approvals`: risk-tiered prompts (§5.3) | `off` / `high_only` / `tiered` | off | high_only | tiered |
| `redaction` (§5.4) | `off` / `warn` (log only) / `enforce` (secrets block, PII ask) | off | warn | enforce |
| `vault`: transcript vault (§4.5) | `off` / `digests` / `full` | off | full | full |
| `audit`: hash-chained audit log (§5.2) | `off` / `on` | on | on | on |
| `audit_anchor`: external checkpoints (§5.2) | `off` / `git` | off | off | git |
| `crypto_shred`: per-conversation keys for reliable deletion (§5.5) | `off` / `on` | off | on | on |
| `backup_purge`: purge rewrites backups (§4.8) | `off` / `on` / `ask` | off | ask | on |
| `mcp_pinning`: tool pinning and rug-pull checks (§4.2) | `off` / `on` | off | on | on |
| `egress_report`: per-conversation report (§4.9) | `off` / `on` | off | on | on |

- **Open** keeps the audit log on, because it costs nothing in daily use. You can still switch it off. With every setting off, Cairnward is a plain router: virtual models, backends and the dashboard's model views keep working.
- **Opt-in features stay opt-in in every preset:** locking a conversation with a passphrase, database encryption (D80), and TLS inspection. No preset locks or encrypts anything by itself.
- **Not switchable,** because they are functional, not protective: the passphrase dialog when you lock or unlock something, and the loopback-only binding and Host and Origin checks of the dashboard and endpoints (§3.1). Switching those off would expose Cairnward to any web page or network peer, not reduce friction. (See open question 0 in §12.)

**Where settings live**
```toml
# cairnward.toml
[privacy]
preset = "balanced"          # open | balanced | strict
approvals = "tiered"         # individual override → shows as "balanced (custom)"

# environments/scratch.toml
[privacy]
preset = "open"

# environments/confidential.toml
[privacy]
preset = "strict"
vault = "digests"            # keep only digests of conversations
```

Resolution order: environment setting → environment preset → global setting → global preset. Virtual models carry the environment (`cw/scratch:low`, `cw/thesis-confidential:high`), so picking a model in any client also picks its privacy level. Coding-agent runs use the environment given to `cairnward run --env`.

**First run.** `cairnward setup` asks for a preset. It describes each preset in one line and says what it needs installed (for example, Strict needs bubblewrap, and a private git repository for anchors). It then checks the prerequisites with `cairnward doctor`. A setting whose prerequisites are missing is reported, and shows as *requested but inactive* until they are fixed.

**Visibility and logging of weakening** (D58)
- `cairnward privacy show [--env NAME]` prints the effective value of every setting, where it came from (preset, global, environment), and which settings are weaker than Strict.
- The dashboard header and `cairnward status` show a badge per environment: `Strict`, `Balanced`, `Open`, or `Custom (3 weaker than Strict)`. Settings that are off or inactive are listed when you open the badge.
- Every settings change is an audit record (`settings.changed`: before, after, who, where). Switching `audit` off first writes a final `audit.disabled` record. Switching it on again writes `audit.enabled` with the length of the gap, so gaps are visible in the chain.
- Where a combination silently weakens a setting, Cairnward explains it. For example, `network = "allowlist"` with `isolation = "off"` shows *advisory only: tools can bypass the proxy*. `audit_anchor = "git"` needs `audit = "on"`. Redaction covers only traffic that Cairnward ends itself.
- A `[storage] key` reference (D80) and plugin weakenings (`unsandboxed`, `unpinned_tools`) also show as warnings, and are recorded in settings snapshots.

### 5.1 Threat model
See [threat-model.md](threat-model.md). It treats third-party clients and agents as untrusted (boundary B7), and records each review in [`reviews/`](reviews/).

### 5.2 Audit log (D8, D37, D49)
- **Records.** A hash chain of records: actor (client, agent, run or user), action, target, decision and digests. Content lives in the vault, referenced by digest.
- **Anchoring** (D49). Every hour (configurable) and on demand, Cairnward commits the latest checkpoint to a **private git repository that you own** (for example `github.com/<you>/cairnward-anchors`). A checkpoint holds a sequence number, a hash and a signature, no content. A deploy key with write access to that repository only does the push.
  - `cairnward audit anchor --setup` makes two OpenSSH Ed25519 keys (a deploy key to push, a signing key) and explains the repository setup.
  - Checkpoints `{seq, hash, time, installation}` are signed with `ssh-keygen -Y sign` and committed as `anchors/<installation>/checkpoints.jsonl`, next to the public `signers` file.
  - `serve` anchors every `[anchor] interval_minutes` (60) when `audit_anchor = "git"`, and skips when nothing is new. Each attempt is audited (`audit.anchor`).
  - `cairnward audit verify --anchors` fetches the repository. It checks the signatures, that each anchored record still has its anchored hash, and how many records came after the last anchor. `doctor` warns when anchoring is on but not set up, or failing.
  - Needs git and openssh-client.
  - **Limit** (T-03). Someone with full control of the machine can still change records **newer** than the last anchor, or stop anchoring. The gap shows as missing anchors. Merkle checkpoints (below) do not change this.
- **Merkle checkpoints** (D79). Each checkpoint (`"v": 2`) also signs `root`: the RFC 9162 Merkle root (as in Certificate Transparency) over records 1 to seq. The leaves are the records' own hashes, with SHA-256 and the 0x00 and 0x01 leaf and node prefixes.
  - `audit verify --anchors` recomputes every root from the log. It checks with a consistency proof that each checkpoint's tree extends the previous one. Version 1 checkpoints (hash only) still verify.
  - `cairnward audit prove <seq> [--out FILE]` writes a self-contained proof: the record (metadata and digests only), its audit path (about log2(n) hashes) and the signed checkpoint.
  - `cairnward audit check-proof FILE --signers FILE` checks the proof without the log or any configuration: the record's hash, its inclusion, and the signature against a signers file that the checker trusts. Without `--signers` it exits with code 2: signature not checked.
  - You can therefore show one record to an auditor without handing over the log.
  - Tested: the tree against the RFC 6962 reference roots. Every proof for trees up to 70 leaves (Hypothesis), including tampered, moved, short and long proofs. Anchoring end to end with a local git repository and real `ssh-keygen` signatures.
- **Independent verifier** (D81). `cairnward-audit-rs` is written apart from the Python writer, so a mistake in either shows up as a disagreement.
  - `verify FILE` recomputes every record's hash, the chain and the sequence numbers from a JSON-lines export (`cairnward audit log --json --last N`, or `audit.jsonl` when the database is not encrypted).
  - `root FILE SIZE` gives the Merkle root over the first SIZE records.
  - `check-proof FILE` checks a proof from `audit prove`. The signature is left to `ssh-keygen -Y verify`.
  - Tested against generated records.
- **Third-party timestamps** (D69). Optional RFC 3161 timestamps on checkpoints (`[anchor] tsa_url`).
- **OpenTelemetry export** of model and tool calls, to a collector on this machine. Off by default.

### 5.3 Policy and approvals (D46, D54)
- **Decisions.** `allow`, `ask` or `deny`, per action type, environment and client.
- **Approvals inbox.** "Ask" decisions appear in the dashboard, as a desktop notification, and in `cairnward approvals`. The request waits. A client may time out on a long wait: the inbox then shows "gave up" for 10 minutes, and answering yes approves the client's next identical request once, so a retry does not ask again (FR-POL-5).
- **Risk tiers** (D54), to keep prompts rare:
  - *low* (reads in the workspace, GET to an already-approved host): allowed and logged
  - *medium* (a new host, a write outside artifacts): asked once per host or scope, with "remember for this environment"
  - *high* (user-only actions, a hosted backend, sending detected secrets): always asked, never remembered.
  Similar pending approvals are answered together (`a` in `cairnward approvals`).
- **How "ask" works with `approvals`** (T0.8).
  - A setting that explicitly says *ask* (`network = "ask"`, `hosted_backends = "ask"`) always asks, whatever the approvals level, because you chose it.
  - Risk-tier asks follow `approvals`: `off` allows, `high_only` asks for high risk, `tiered` asks for medium and high.
  - When nothing can ask you (no terminal, no dashboard), the answer is **deny**.
  - `cairnward policy explain KIND TARGET [--env E]` shows a decision and its reason without running anything.
- **User-only actions** (D46): `encrypt` and `decrypt`, sync changes, enabling hosted backends, adding MCP servers, runs without a sandbox.
  - They are agent tools, but each call opens a **confirmation dialog in Cairnward's own interface**, never in the client.
  - The dialog shows the action, the conversation, and its **provenance**: the content that entered the context since your last message (for example "after reading `paper.pdf` page 4"). If anything untrusted came after your last message, the dialog says so prominently.
  - You type passphrases only in this dialog, or in `cairnward approvals` (twice, hidden). A passphrase goes to the vault without being stored, logged or audited.
  - **When the model starts a user-only action, Cairnward always asks, even with `approvals = "off"`,** and never remembers the answer. Prompt injection therefore cannot switch protections off unseen. When you start the action yourself, it follows the `approvals` setting.
- **Provenance tracking** (T2.6). The tracker notes tool results after your last message (from chat requests) and tool outputs relayed by the MCP proxy, per client and run. The first user-only tool is `cairnward__lock_conversation` (opt-in with `tools = ["vault"]`), which locks the client's current conversation after confirmation.

### 5.4 Redaction
- **Scope.** Traffic that Cairnward ends itself: model calls to non-local backends (messages and embedding inputs), MCP tool arguments, and plugins' fetches.
- **Scanner** (T2.1). Built-in patterns for:
  - secrets: private keys, AWS, GitHub, OpenAI, Anthropic, Slack, Google, Stripe and Hugging Face tokens, JWTs, Cairnward keys, password assignments
  - personal data: emails, phone numbers, payment cards (Luhn check), IBANs (checksum), US SSNs, NRIC-style ID numbers
  - your own `[redaction] custom` patterns, and `ignore` rules, in `cairnward.toml`.
  **Presidio** is an optional worker for richer detection of personal data (D53).
- **Modes.**
  - `warn`: the request is sent, flagged in `X-Cairnward-Warning`, and audited.
  - `enforce`: secrets are refused (`403 redaction_blocked`, or a tool error). Personal data asks (`data.send`), and the answer can be remembered per conversation or environment.
- **What is recorded.** Audit records hold the kinds and counts, never the matched text. Content is never changed.

### 5.5 Encryption and deletion
**Per-conversation keys** (T0.5, [ADR-0006](adr/0006-encryption-age-key-wrapping-crypto-shredding.md)).
- Conversations live in `vault.db`, their keys in a separate small `keys.db`. Both are SQLite with WAL, `synchronous=FULL` and `secure_delete=ON`.
- Each conversation has its own age key. Purging deletes the key first, then the content, then checkpoints both files, and leaves a tombstone row.
- With `crypto_shred = "off"`, content is stored as plain text.
- Deleted keys are overwritten inside the database files. Whether the SSD still holds old blocks is outside SQLite's control. Locked conversations, database encryption or full-disk encryption are the strong guarantee.
- Copies in clients' own histories are outside Cairnward's control.

**Locks** (T2.5).
- `cairnward vault lock <id>` wraps the conversation's key with a passphrase (age scrypt, at least 8 characters) and overwrites the plain key in `keys.db`.
- A locked conversation's title and messages cannot be read, and it is not searched. It is **never sent to hosted backends, even while unlocked** (`403 locked_conversation`).
- `vault unlock <id> [--minutes 30]` opens it in the running core, in memory only, so new exchanges are stored again. `vault relock` closes them all. `vault show` and `export` ask for the passphrase in the terminal. `unlock --permanently` removes the lock.
- While a conversation is locked, a local model still answers, but the exchange is not stored, and the client gets an `X-Cairnward-Warning`.
- Passphrases are read with `getpass`, or arrive on the owner-only management socket. They are never stored, logged or audited (tested by scanning every file that Cairnward writes).
- A forgotten passphrase can be bypassed only with the optional recovery key (§4.5).

**Database encryption** (D80, [ADR-0019](adr/0019-sqlcipher-encrypt-everything.md)). `cairnward store encrypt`, with `serve` stopped, puts `vault.db`, `keys.db` and `audit.db` under SQLCipher.
- **What it protects.** One random 256-bit key encrypts every page of the three files. Titles, folders, timestamps, IDs, sizes, the audit records and the unlocked conversations' keys are then not readable from the files. Per-conversation keys and crypto-shredding stay as above, inside it.
- **The key.** `<data_dir>/db-key.age` holds the key, wrapped with a passphrase (age scrypt, at least 8 characters).
  - `serve`, and every command that opens the databases, asks for the passphrase on a terminal.
  - For unattended starts, `[storage] key = "env:NAME" | "keyring:NAME"` in `cairnward.toml` may hold the key itself (64 hex characters). This is a weakening: `serve`, `doctor` and the dashboard warn about it, and settings snapshots record it.
  - In isolated mode, the outer process writes the key to the sandboxed core's standard input, which the core reads once at start. The key is never in an environment, because `/proc/<pid>/environ` would keep it (review 2026-11-10, R1).
  - An `env:` reference is removed from the environment once read, so programs started afterwards do not inherit it (R2). Agents, MCP servers and plugins never get the key.
- **Crash safety.** The wrapped key is saved as `db-key.age.pending` before any file is converted. Running `store encrypt` again finishes an interrupted run, with the same passphrase.
- **Other commands.** `store passphrase` changes the passphrase (the key and the files stay the same). `store decrypt` switches encryption off. `store info` and `doctor` report the state.
- **The audit mirror.** An encrypted audit log keeps no plaintext `audit.jsonl` mirror, because the mirror would hold what the encryption hides. Encrypting removes it, decrypting writes it again, and `audit verify` notes that it is missing. Export with `audit log --json`.
- **Backups** read the encrypted databases with the key. The audit copy stays encrypted, and the wrapped key file goes into the snapshot with it.
- **Platform.** Needs the `sqlcipher` extra (`sqlcipher3-binary`). **Linux x86-64 only.**
- **Cost** (measured on the build machine). Vault writes are about 20% slower, and reading a conversation about 7% slower. Audit appends got faster, because the mirror file is not written.
- **Tested** with the real driver:
  - content, schema and log targets are absent from the files
  - wrong, missing and short keys are refused
  - migrations, WAL and `secure_delete` work on encrypted files
  - conversion both ways, an interrupted encryption finished, a passphrase change, a key by reference, and backups of encrypted databases. **In isolated mode, with the real sandbox** (`tests/isolation/test_encrypted_storage.py`): the core opens the encrypted databases, and the key is in no core or wrapper process's `/proc/<pid>/environ`. **Not tried yet:** replication with encrypted databases.
- **Limits.** Converting does not erase earlier plaintext from the disk's free space or from old backups, so full-disk encryption is still advised. Tools that read the files directly stop working.

### 5.6 Supply chain
- Hash-locked dependencies: `uv.lock`, `redaction_engines/locks/`, `tests/e2e/package-lock.json`, `rust/Cargo.lock`.
- CI checks: pip-audit, `cargo audit --deny warnings`, npm audit for the sandbox, a license check of runtime dependencies, pinned CI actions, CodeQL.
- Releases carry an SBOM (CycloneDX) and Sigstore signatures. The release workflow has not run yet (status A8).
- Pinned MCP servers and plugins.
- Model files: GGUF and safetensors only.

**Model downloads** (T2.8). `cairnward models pull <url> --sha256 <hex> [--name] [--env]` downloads one GGUF or safetensors file through `cairnward-egress`, with a short-lived session from the running core.
- Every host, CDN redirects included, is checked against that environment's network policy, and you can approve it in the inbox.
- The file goes to a private partial file. It is kept only if its SHA-256 matches and its header is a real GGUF or safetensors header.
- It is recorded as `model.download` in the audit log and in `models/manifest.json`. `cairnward models list` shows verified files.
- Ollama's own `ollama pull` still downloads directly from its registry, outside Cairnward. Importing a verified GGUF into Ollama (`FROM <path>` in a Modelfile) avoids that.

---

## 6. Technology choices

| Area | Choice | License |
|---|---|---|
| Core | Python 3.12+, FastAPI, Uvicorn, pydantic | MIT, BSD |
| Egress proxy | Python (`cairnward-egress`). Opt-in Rust data path (`cairnward-egress-rs`: tokio, rustls with ring, serde_json) | —, MIT / Apache-2.0 |
| Audit verifier | Rust (`cairnward-audit-rs`: serde_json, sha2) | MIT / Apache-2.0 |
| Sandbox | sandbox-runtime (`srt`, pinned: bubblewrap, seccomp, proxies, needs Node.js 22.12+). Podman or Docker for server-style clients | Apache-2.0, LGPL-2.0+ |
| Store | SQLite (WAL), Alembic migrations. Optional SQLCipher (`sqlcipher3-binary`) | Public domain, MIT, MIT |
| Crypto | age through pyrage. Replication: ChaCha20-Poly1305, Ed25519 (cryptography) | MIT, Apache-2.0 |
| MCP | Official MCP Python SDK (D65, [ADR-0015](adr/0015-mcp-proxy.md)) | MIT |
| Redaction | Built-in rules. Presidio as an optional worker | —, MIT |
| Backup and sync | restic, rclone, git (separate processes) | BSD-2, MIT, GPL-2.0 |
| Dashboard | Plain ES modules, no build step ([ADR-0016](adr/0016-dashboard-without-a-build-step.md)) | — |

---

## 7. Hardware and platforms
- **Memory budget at 8K context:** main model 18.2 GB, fast model 2.3 GB, embeddings about 0.6 GB, vision on demand (unloads the fast model). That leaves about 8 GB for the operating system, Cairnward, a client and light desktop use.
- **Measured on the reference machine** (D59, [results](bench/results-2026-11-01-ryzen3-3200g.md)): Ryzen 3 3200G, 4 cores, 29.3 GB usable RAM, no GPU, Debian 13.
  - `qwen3:30b`: reads about 20 tokens/s. Writes 11 tokens/s, falling to 2 as the context grows to 4K. 18.2 GB at 8K context. Loads in 33 s when the operating system has cached it, 74 s cold.
  - `qwen3:1.7b`: reads about 50 tokens/s. Writes 17 tokens/s, falling to 6.5. 2.3 GB. Loads in about 2 s.
  - `qwen3:4b`: reads about 21 tokens/s, the same as the 30B MoE. Writes 10 tokens/s, falling to 2.6. 3.6 GB. A small dense model is faster on a CPU only when it has fewer *active* parameters than the MoE model (about 3B).
  - Follow-up turns with an unchanged prompt start: 18.6 times faster (11.7 s against 217 s at 4K).
  - The main and fast models fit together at 8K context only when heavy desktop apps are closed (about 9 GB left).
- **Consequences:** 8K default context (D60). First turns of long prompts take minutes, follow-ups seconds (D62). Heavy coding agents are slow (D64).
- **Upgrade paths** (for users, not required):
  - an 8-core AM4 CPU (for example Ryzen 7 5700X, if the motherboard BIOS supports it): prompt reading about 2–3 times faster
  - a GPU with 12 GB VRAM or more: prompt reading many times faster, with llama.cpp's MoE offload (experts on the CPU, the rest on the GPU)
  - 64 GB RAM: larger contexts and more models loaded, not faster.
- **Performance targets** come from `scripts/bench_backend.py` results (§8).
- **Platforms.** Linux first (Debian 12+, Ubuntu 22.04+, Mint, Pop!_OS). Windows through WSL2 (D74, [guide](wsl2.md)): Cairnward and sandboxed agents run inside WSL2, and Windows clients connect over loopback. CI runs the unit tests on Windows too, so that nothing becomes harder to port.

---

## 8. Non-functional requirements (summary)
The full list is in [requirements.md](requirements.md).

| Area | Target |
|---|---|
| Gateway overhead | 75 ms or less (150 ms or less with isolation) added per model request under Strict (policy, audit, vault write), not counting model time |
| Model speed (measured, reference machine) | A baseline that Cairnward does not control: main model about 20 tokens/s reading, 2–11 writing. Fast model about 50 reading, 6.5–17 writing |
| Cache reuse | A follow-up turn in an unchanged conversation gets its first token at least 10 times faster than a cold turn (measured 18.6 times). The gateway must never break this |
| Responsiveness | Fast-model virtual models start answering a 512-token prompt within 15 s (measured 10 s). Main model within 30 s (measured 21 s) |
| Waiting-time warning | Requests expected to need more than 60 s before the first token are flagged, using the stored benchmark profile |
| Footprint | Core 500 MB RAM or less, without optional workers |
| Isolation | With `isolation = "on"`, direct connections from any sandbox fail (`cairnward doctor` and CI tests) |
| Reliability | No data loss on a crash. An audit record before every effect, when audit is on |
| Privacy | Strict: no outbound connections unless allowlisted. Balanced: none without approval. Open: as configured |
| Optionality | Every protection can be switched off on its own, and Cairnward still works. With all of them off, gateway overhead is 10 ms or less per request |
| Accessibility | Dashboard WCAG 2.2 AA. Single-key shortcuts can be switched off |
| Portability | Linux first. Windows through WSL2 (not yet run on a real machine). Python 3.12+ |

## 9. Delivery plan (summary)
Details in [plan/phase0.md](plan/phase0.md).
- **M0, gateway skeleton:**
  - the privacy settings model, with presets and per-environment overrides
  - the model endpoint, passing through to Ollama with policy, audit and vault write
  - the `cairnward run` sandbox, with no direct network
  - `cairnward-egress`
  - one coding agent and one chat client working through it.
- **M1, research core:** moved to the research plugin since.
- **M2, security features and dashboard:** approvals inbox, risk tiers, provenance dialog, redaction, egress report, vault browser, locking, audit anchoring, the MCP proxy with pinning, all four coding agents and three clients tested.
- **M3, sync and connectors:** hardening, the MCP SDK, backups with purge rewrite, hosted backends ([plan/m3.md](plan/m3.md)). Multi-device replication (D72), deferred by D71, was built later.
- **Built after M3 (2026-11-10):** Merkle checkpoints (D79), database encryption (D80), and the Rust plan of [ADR-0020](adr/0020-rust-egress-and-audit.md): an opt-in egress data path with TLS inspection, and an audit verifier (D81).
- **Later, no date:** an Ollama-compatible endpoint. Switching the egress default to Rust, after it has run for real (status A18).

## 10. Testing strategy
- **Unit and property tests** for every component, with real sockets for anything that talks HTTP or starts a worker.
- **Isolation tests:** every sandbox type tries direct DNS, TCP and UDP, and must fail (`tests/isolation/`, in CI with the real sandbox).
- **Client compatibility tests:** scripted sessions for each tested client and coding agent, against a stand-in model server.
- **Vault reconstruction tests** (property-based): any sequence of stateless requests rebuilds the same conversation tree.
- **Anchor tests:** tampering older than the last anchor is detected.
- **Backup purge tests:** purged content is absent from all restic snapshots afterwards.
- **Differential tests:** the Rust egress data path and audit verifier must agree with the Python ones.
- **Browser test:** the dashboard in Chromium, with axe-core.

## 11. Data and configuration contracts
- TOML configuration validated by pydantic, with JSON Schemas committed in `schemas/` (`cairnward config schema`).
- Alembic migrations for the databases.
- OpenAPI for the gateway (`api/openapi.json`).
- A versioned format for replication logs and audit checkpoints.
- The client profile format for `cairnward run` and `cairnward client add`.

## 12. Open questions
- **0a.** Does Ollama's flash attention (without KV quantisation) reduce the slowdown of generation as the context grows, on this CPU? If so, `cairnward doctor` recommends it. Optional.
- **0.** Should the dashboard and endpoint hardening (loopback-only binding, Host and Origin checks) also be switchable? Currently not, because switching it off exposes Cairnward rather than reduce friction (§5.0).
- **4.** Repository rename to `cairnward`: done.
- **5.** Drive folders for the first mirror: decided at setup time.
- **Closed:**
  - the third tested chat client: LibreChat (T2.9)
  - reusing sandbox-runtime: yes ([ADR-0013](adr/0013-reuse-sandbox-runtime-for-isolation.md))
  - the literature engine: moved to the plugin
  - the MCP layer and the dashboard framework: D65, D66.

## 13. History
- **v0.1–v0.5:** an all-in-one local agent with Claude-app features. See the git history of this file.
- **Critique (2026-10-31):**
  - the scope was too large for one person
  - most chat and coding features already existed (Open WebUI, Jan, opencode, Qwen Code, Aider, Goose, PaperQA2, Zori, ZotLLM)
  - egress control inside one process could not be enforced
  - the audit log was not tamper-evident without external anchors
  - backups defeated crypto-shredding
  - there were heavy dependencies
  - the name was taken on PyPI.
  v0.6 answers each point (D42–D54).

## 14. Risks
- **Weaker configurations look protected.** A user on Open or Custom may expect the guarantees of Strict. Mitigation: the badge, `privacy show`, *advisory only* warnings for combinations that do not work, and an audit record of every change.
- **Client compatibility drift.** Clients and agents change their APIs and configuration often. Mitigation: profiles, compatibility tests, and the generic OpenAI-compatible path.
- **Approval waits against client timeouts.** A long "ask" wait can time out a client. Mitigation: risk tiers, remembered approvals, and approvals that apply to the retry.
- **Content redaction blind spot.** Egress filters third-party tools' own HTTPS by host only, unless TLS inspection is on. Mitigation: network denied by default, per-host approval, the egress report, documentation.
- **The vault duplicates client data.** Conversations exist twice (vault and client). Mitigation: setup guides that keep client history small, and a vault that you can lock and purge.
- **CPU speed** (about 20 tokens/s prompt reading on the main model). Interactive coding with heavy agents is slow. Mitigation: cache-aware scheduling, the 1.7B fast model, Aider first, waiting-time warnings, optional hosted and LAN backends.
- **Prompt injection.** Its impact is limited by sandboxing, a network denied by default, provenance dialogs and the audit log. It cannot be fully prevented.
- **Sandbox bugs** (a bubblewrap or namespace misconfiguration) would silently weaken the main guarantee. Mitigation: `cairnward doctor` isolation self-tests, and isolation tests in CI.
