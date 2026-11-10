# Spec: Cairnward — a local privacy gateway

Status: **Draft v0.8, for review**
v0.8: defaults and performance targets set from the reference-machine benchmark ([results](bench/results-2026-11-01-ryzen3-3200g.md)): 8K default context, 1.7B fast model, cache-aware scheduling, tiered research pipeline (D59–D64).
v0.7: **every privacy and security feature is optional**, chosen through presets (Open / Balanced / Strict) with per-environment overrides (§5.0, D55–D58).
v0.6 changed direction after the [project critique](#13-history): Cairnward is now a **client-neutral privacy gateway** that existing chat apps and coding agents plug into, plus **its own literature-review workflows**. Earlier versions specified an all-in-one Claude-app clone; decisions that no longer apply are marked *superseded* in §2.
Date: 2026-10-31
Repo: `c23075/possessed-incantation` (to be renamed `cairnward`)
Name: **Cairnward**: cairns mark a trail (the audit trail); *ward* means to guard. Command `cairnward`, short alias `cw`. License: **Apache-2.0**.

**Companion documents**
- [`requirements.md`](requirements.md): numbered requirements (FR-/NFR-) with MoSCoW priority and verification.
- [`threat-model.md`](threat-model.md): STRIDE + LINDDUN, mapped to OWASP Top 10 for LLM Applications (2025).
- [`adr/`](adr/): Architecture Decision Records.
- [`plan/phase0.md`](plan/phase0.md): milestones and ordered tasks.
- [`bench/`](bench/): benchmark instructions and results (`scripts/bench_backend.py`).

---

## 1. Purpose

Most local AI tools (chat apps, coding agents) run on your machine but give you **no verifiable record of what they did and no enforced control over what leaves the machine**. Cairnward sits between those tools and the outside world:

1. **Gateway:** one local endpoint for models (OpenAI-compatible) and tools (MCP). Every model call, tool call and outbound connection is checked against a policy, recorded in a tamper-evident audit log, and shown in an egress report.
2. **Enforced isolation:** tools run inside a sandbox whose **only** network route is Cairnward's egress proxy. This is enforced by the operating system, not by the tools' good behaviour.
3. **Transcript vault:** Cairnward keeps its own copy of every conversation that passes through it, which you can search, lock with a passphrase, sync and reliably delete, whichever client you used.
4. **Plugins under a contract (D75):** other programs can run sandboxed with no network of their own, and ask Cairnward for models and outside requests. The first is a separate project for literature review.

Primary use cases: **coding** (via existing coding agents run through the gateway) and **research** (through a plugin, or any client), equally.

All of these protections are **optional** (§5.0). With everything switched off, Cairnward is a plain model router with virtual models; each protection can be switched on globally or per environment.

### 1.1 Goals
- G0. **User choice:** every privacy and security feature can be switched on or off, globally or per environment, through presets or individually; weakening is visible and logged, never silent.
- G1. Work fully offline with open-weight models (Qwen, DeepSeek) on a CPU-only 32 GB machine.
- G2. **Enforced choke point (when isolation is on):** no tool, client or Cairnward component can reach the network except through the egress proxy (OS-enforced).
- G3. Verifiable record: audit log with externally anchored checkpoints.
- G4. Client-neutral: any OpenAI-compatible chat client or coding agent works; a tested set is documented.
- G5. Anything beyond the gateway is a separate plugin (D75), under a contract any plugin follows.
- G6. Pluggable model backends (local, LAN, hosted), with hosted denied by default.

### 1.2 Non-goals
- Building a chat app or a coding agent (use existing ones through the gateway).
- Multi-user/team server, remote access, cloud hosting.
- Training or fine-tuning.
- Native Windows (Linux first; Windows through WSL2, D74).

---

## 2. Decisions log

Current decisions. Superseded decisions are listed at the end with what replaced them. Major decisions have ADRs.

| # | Topic | Decision | ADR |
|---|---|---|---|
| D42 | Product direction | **Gateway + research**: client-neutral privacy gateway (model endpoint, MCP proxy, egress proxy, policy, audit, vault, dashboard) + own literature-review workflows | [0009](adr/0009-gateway-pivot.md) |
| D43 | Use cases | Coding and research equally; coding via existing agents (Qwen Code, opencode, Aider, Goose, any OpenAI-compatible agent) run through the gateway | 0009 |
| D44 | Clients | Client-neutral; 2–3 chat clients documented and tested; Cairnward builds only a **dashboard** (audit, egress, approvals, vault, settings) | 0009 |
| D45 | Egress enforcement | **OS-level**: sandboxed processes have no network except a bridged connection to the egress proxy; Cairnward's own components run the same way; only the small `cairnward-egress` process has real network access | [0010](adr/0010-os-level-egress-enforcement.md) |
| D46 | User-only actions | Agent tools with a **confirmation dialog** in the dashboard/CLI that shows the action **and its provenance** (what content preceded the request) | — |
| D47 | Chat data | **Transcript vault**: Cairnward stores every conversation passing through it; lockable, syncable, purgeable | [0011](adr/0011-transcript-vault.md) |
| D48 | Effort and environment selection | **Virtual models** (`cw/<environment>:<effort>`) so any client's model picker selects effort + environment | — |
| D49 | Audit anchoring | Checkpoint hashes committed periodically to a **private git repo** the user owns | [0008](adr/0008-hash-chained-audit-log.md) (amended) |
| D50 | Backup purge | Purging a conversation also **rewrites restic snapshots** to remove it, then prunes | [0007](adr/0007-sync-restic-backup-and-event-log-replication.md) (amended) |
| D51 | Name, license | **Cairnward** (`cairnward`, alias `cw`), **Apache-2.0** | — |
| D52 | Platforms | **Linux (Debian family) first**; Windows via WSL2 (D74); CI keeps unit tests on Windows | — |
| D53 | Heavy dependencies | Lean core; Presidio and other heavy libraries run as **optional workers** | — |
| D54 | Approvals | Risk-tiered, batched approvals with an approvals inbox, to avoid approval fatigue | — |
| D55 | Optional protections | **Every** privacy/security feature can be switched off, including isolation, network blocking, approvals, redaction, vault, audit log, anchoring, crypto-shredding, backup purge rewrite and MCP pinning | [0012](adr/0012-optional-privacy-controls-and-presets.md) |
| D56 | Defaults | **Preset chosen at first run** (`cairnward setup`): Open, Balanced or Strict; changeable any time; Custom when individual settings differ | 0012 |
| D57 | Granularity | **Global settings + per-environment overrides**; a virtual model (`cw/<env>:<effort>`) therefore also selects the privacy level | 0012 |
| D58 | Weakening | Allowed without friction, but **visible** (dashboard badge, `cairnward status`, `cairnward privacy show`) and **logged** (audit record of every change, written before audit itself is switched off) | 0012 |
| D59 | Reference machine | Ryzen 3 3200G (4 cores), 29.3 GB usable RAM, no GPU, Debian 13. Main model reads prompts at ~20 tok/s, writes at 2–11 tok/s; 1.7B model reads at ~50 tok/s | — |
| D60 | Default context | **8K** tokens (16K allowed with a warning; 32K only on machines with more RAM/speed) | — |
| D61 | Default models | Main: `qwen3:30b` (30B-A3B MoE); **fast: `qwen3:1.7b`** (2.3 GB, loads in 2 s, reads 2.5× faster). `qwen3:4b` measured: no faster than the 30B MoE on CPU, so it's only offered as a low-memory main model | — |
| D62 | Cache-aware gateway | Never alter a conversation's prompt prefix; keep the main model loaded while in use; **conversation affinity** so follow-up turns reuse the cache (measured 18.6× faster); save/restore caches per conversation where the backend supports it (llama.cpp slots) | — |
| D63 | Tiered research pipeline | Moved to the research plugin with ADR-0017: see the plugin's `docs/design.md` | [0017](adr/0017-research-as-a-plugin.md) |
| D64 | Coding agents on this hardware | Lean agents first (**Aider**); heavy-prompt agents (Qwen Code, opencode, Goose) supported but flagged as slow on CPU (first turn of a ~10K-token prompt ≈ 8 min) | — |
| D65 | MCP protocol layer (2026-11-02) | The proxy's protocol plumbing is the **official MCP Python SDK** (typed layer, both directions; client endpoint stateless); Cairnward's policy, approvals, pinning and audit stay as they are; only standard fields reach clients (non-standard ones dropped and audited); pins hash the canonical JSON of what clients get, old pins carried over only when unchanged | [0015](adr/0015-mcp-proxy.md) (amended) |
| D66 | Dashboard code (2026-11-02) | **Keep plain ES modules without a build step** (no npm supply chain in the page where approvals happen); revisit only if the dashboard grows a lot | [0016](adr/0016-dashboard-without-a-build-step.md) |
| D67 | Reference formatting | Moved to the research plugin with ADR-0017: see the plugin's `docs/design.md` | [0017](adr/0017-research-as-a-plugin.md) |
| D68 | Richer PDF parsing | Moved to the research plugin with ADR-0017: see the plugin's `docs/design.md` | [0017](adr/0017-research-as-a-plugin.md) |
| D69 | Third-party timestamps | Audit checkpoints can also be timestamped by an **RFC 3161 timestamp authority** (opt-in; only the checkpoint's hash leaves the machine) | 0008 |
| D70 | Hosted providers | OpenRouter, DeepSeek API, Alibaba Model Studio, Groq, Cerebras, Gemini; denied by default, never for locked conversations. Backends that **may train on prompts** (most free tiers) are marked so and by default allowed **only in environments marked scratch**; the restriction can be switched off (visible and audited, like every weakening) | — |
| D71 | M3 scope and order (2026-11-02) | Hardening first (server-mode test matrix, fuzzing, one-command eval/checklist runner, OpenShell review), then MCP SDK + citeproc-py, restic backups, Zotero + academic APIs, full review pipeline, Docling/GROBID, hosted providers, Drive mirror. Presidio and OpenTelemetry export included. **Deferred:** multi-device replication, multi-agent YAML workflows | [plan/m3.md](plan/m3.md) |
| D72 | Replication (2026-11-04, FR-SYNC-4) | Through a **shared folder** (Syncthing, NAS, synced drive), one append-only signed and encrypted log per device and conversation; a device's file is deleted when the conversation is purged, so a purge reaches the folder. Concurrent turns fork inside the conversation's existing message tree rather than making a second conversation. New devices need approval. No git remote or rclone transport | [0007](adr/0007-sync-restic-backup-and-event-log-replication.md) (amended) |
| D73 | What agent runs can read (2026-11-04) | `home_access`: with `hidden`, the default in Balanced and Strict, a run sees only the parts of the home folder it needs, because a list of secret folders can't cover documents and other projects; `readable` is the opt-out, shown as a weakening. A run key is also pinned to its environment, because the environment selects the privacy level | — |
| D74 | Windows (2026-11-04, NFR-PORT-2) | The owner uses a Windows laptop, so WSL2 moves from Won't to Should. Cairnward runs inside WSL2 with the Linux sandbox; WSL1 is refused. Under WSL every sandbox hides the Windows drives (from the mount table) and `/init` and `/run/WSL`, because a Windows program would run outside the sandbox. Cairnward's own folders may not be on a Windows drive. No native Windows isolation | — |
| D75 | Research as a plugin (2026-11-05, [ADR-0017](adr/0017-research-as-a-plugin.md)) | Accepted: research moved to its own repository and runs under a general plugin contract. D42 and D43 are amended: coding and research stay equally important uses, but research is a consumer of the gateway. Choices the ADR left open: the workflow runner moved with research (it only runs research steps); backups stay in the outer process for now; no job page in the dashboard (the plugin's tools and CLI report jobs); plugins come only from a path the user names | — |
| D77 | OpenShell as a second sandbox (2026-11-05) | Opt-in per environment (`sandbox = "openshell"`, isolation on). The OpenShell policy allows only Cairnward's gateway and egress ports, so Cairnward stays the policy authority. The agent works on a copy of the workspace that is mirrored back. Tested only against a stand-in CLI | [0018](adr/0018-openshell-sandbox-and-tls-inspection.md) |
| D78 | TLS inspection in egress (2026-11-05) | FR-NET-2 built: `[tls_inspection]` per environment, off by default, never set by a preset. A local CA, method and path rules, one audit record per request (path as a digest). The core's own connections are never inspected | [0018](adr/0018-openshell-sandbox-and-tls-inspection.md) |
| D79 | Merkle checkpoints (2026-11-10) | Each anchored checkpoint also signs the RFC 9162 Merkle root over records 1..seq. `audit verify --anchors` recomputes each root and checks each checkpoint extends the one before; `audit prove` / `audit check-proof` show one record is in a signed checkpoint without the rest of the log | [0008](adr/0008-hash-chained-audit-log.md) (amended) |
| D80 | Database encryption (2026-11-10) | Optional SQLCipher for vault.db, keys.db and audit.db, off by default. The key is wrapped with a passphrase asked at start; a `[storage] key` reference is an optional, visible weakening for unattended hosts | [0019](adr/0019-sqlcipher-encrypt-everything.md) |
| D81 | Rust audit verifier, not writer (2026-11-10) | ADR-0020 stage 4 builds an independent verifier (`cairnward-audit-rs`) instead of moving the writer: with D80, a Rust writer would need a second process holding the database key. The verifier recomputes hashes, the chain and Merkle roots from an export | [0020](adr/0020-rust-egress-and-audit.md) |
| D76 | Plugins bring their environments (2026-11-05) | The core ships only `coding` and `scratch`. `litreview-offline` and `litreview-online` moved out: `cairnward-research install-plugin` writes them as ordinary files in `environments/`, and keeps any already there | [0017](adr/0017-research-as-a-plugin.md) |
| D5, D19 | Model backends | Pluggable: Ollama (default), llama.cpp, any OpenAI-compatible server; per role / environment / effort | [0004](adr/0004-pluggable-backends.md) |
| D29 | Hosted providers | Denied by default; tested first: Alibaba Model Studio, DeepSeek API, OpenRouter; Claude API later (separate API key) | 0004 |
| D7, D25 | Literature sources, citations | Moved to the research plugin with ADR-0017: see the plugin's `docs/design.md` | [0017](adr/0017-research-as-a-plugin.md) |
| D9 | Stack | Python now; egress proxy and audit log are first candidates for Rust | [0003](adr/0003-python-now-rust-later.md) |
| D11, D21 | Hardware | 32 GB RAM, CPU only; 32K default context, adjustable | — |
| D12, D20 | Effort levels | 3 defaults + user-defined levels in `effort.toml` | — |
| D13 | Environments | Profile bundles (workspace, tools, network policy, sandbox, models) | — |
| D15 | Skills/tools | SKILL.md folders + MCP servers with permission manifests (served through the MCP proxy) | — |
| D22 | Artifacts | Git repo per conversation/job/agent run | — |
| D23, D24, D31, D35, D36 | Encryption | Off by default; user-initiated; per-conversation passphrases; age format with key wrapping; crypto-shredding | [0006](adr/0006-encryption-age-key-wrapping-crypto-shredding.md) |
| D27, D30, D40 | Sync | Modes none/on_demand/auto; restic backup; per-device event-log replication; git for artifacts | 0007 |
| D32–D34, D37–D39, D41 | Practice | ADRs; STRIDE/LINDDUN; local API hardening; OpenTelemetry export; MCP pinning; WCAG 2.2 AA for the dashboard; contracts and supply chain | 0001, [0005](adr/0005-local-core-service-and-api-hardening.md), 0008 |

**Superseded in v0.6:** D1 hybrid all-in-one build → D42; D2 TUI + web chat clients → D44 (dashboard only); D6 own coding flow → D43; D10 multi-agent everywhere → research workflows only (§4.7); D16/D17 Claude-app feature set and "thin slice of everything" MVP → D42 and the milestones in §9; D28 Windows sandbox tiers → D52 (WSL2 later); D26 name "Cairn" → D51; chat folders/shortcuts in own client → vault browser (§4.5).

---

## 3. Architecture

Isolated mode (isolation on), as built. Updated 2026-11-06; the earlier diagram predated the
two-process layout.

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

Each MCP server and plugin runs in its own srt sandbox, started by the outer process (srt can't
nest) and connected to the core over a socket. With isolation off, the outer process and the core
are one process; agents and MCP servers in an environment with isolation on are still sandboxed.

The diagram shows the **Strict** configuration. Each box below the policy engine, and the sandboxing itself, can be switched off (§5.0); the endpoints, routing and virtual models always work. What none of this protects against is in [limitations.md](limitations.md). UML diagrams of the processes, main classes and two request flows are in [architecture-uml.md](architecture-uml.md).

**Rules** (when the corresponding protections are on)
1. Clients and agents talk only to Cairnward (model endpoint, MCP proxy) and, if allowed, to the egress proxy. The OS enforces this when isolation is on (§3.2); with isolation off, the proxy settings are advisory and tools can connect directly.
2. Every effect (model call, tool call, outbound connection, file write by Cairnward) goes through `policy.check → allow | ask | deny`, with an audit record before and after (when audit is on). Settings that are off make the policy engine return `allow` for that category without asking.
3. `cairnward-egress` is the only process with real network access. It is small, does host allowlisting and connection logging, and is the first component to be rewritten in Rust (D9).
4. The local model server is reached through a Unix socket or loopback port that is bridged into the sandbox; it never needs internet access after models are downloaded.

### 3.1 Local API and dashboard security
Unchanged from v0.5 ([ADR-0005](adr/0005-local-core-service-and-api-hardening.md)): Unix socket for the CLI, loopback-only HTTP for the dashboard with Host allowlist, Origin checks, CSRF tokens, `SameSite=Strict` cookies, strict CSP, no CORS, single-use login links. The model endpoint and MCP proxy accept per-client **API keys** issued by Cairnward (`cairnward client add open-webui`), so the audit log knows which client made each request and each client can have its own policy.

### 3.2 OS-level egress enforcement (D45, [ADR-0010](adr/0010-os-level-egress-enforcement.md))
Linux mechanism: Anthropic's open-source **sandbox-runtime** (`srt`, pinned) wraps bubblewrap, seccomp and filtering proxies; `cairnward-egress` is its parent proxy ([ADR-0013](adr/0013-reuse-sandbox-runtime-for-isolation.md); spike results in `spikes/t06-sandbox/`). Requires Node.js ≥ 22.12 when isolation is on. Details:
- **Agents and CLI tools** (`cairnward run -- qwen`) run under **bubblewrap** with a **new network namespace that has only a loopback interface**. Inside it, Cairnward's model endpoint, MCP proxy and HTTP(S) proxy are reachable through Unix sockets bridged to loopback ports. `HTTP_PROXY`/`HTTPS_PROXY` point at the bridge. Anything that ignores the proxy settings simply has no route out.
- Filesystem: the workspace is mounted read-write; the agent's own config directory read-write; the home folder hidden except what the run needs (`home_access`, §4.3); the backup and replication folders hidden; the rest of the system read-only.
- **Server-style clients** (Open WebUI, LibreChat) run in **Podman/Docker containers on an internal network** whose only member besides the client is Cairnward. Templates (`compose` files) are provided.
- **Cairnward itself** runs its core in the same kind of namespace, so its own libraries (e.g. Docling or Presidio fetching models) can't bypass the proxy. Model and data downloads go through `cairnward-egress` like everything else and are audited.
- **What the proxy can see:** host names (from CONNECT / SNI), bytes and timing for all connections. **Content** is visible only for traffic Cairnward originates or terminates itself (model calls, MCP tool calls, research API calls), so **content redaction applies to those**. For third-party tools' own HTTPS connections, policy is host-level allow/ask/deny only, unless the environment turns on **TLS inspection** (D78, below).
- **Rust data path (opt-in, [ADR-0020](adr/0020-rust-egress-and-audit.md)):** `[egress] implementation = "rust"` runs `cairnward-egress-rs` (built from `rust/`) for connections, parsing, DNS and address checks. It asks a decision service in the Python process (`egress/decisions.py`, a 0600 Unix socket) for sessions, policy, approvals and audit records, so those have one implementation. Behaviour matches the Python data path, and every egress test runs against both. TLS inspection is done in Rust too (rustls; certificates, request verdicts and records still from Python), and every TLS-inspection test runs against both paths. The decision socket requires a secret that the binary gets on stdin, because the isolated core can reach any Unix socket (T-39). Measured at the same cost as the Python path ([bench](bench/results-2026-11-10-egress-rust.md)) and reviewed ([review](reviews/rust-data-path-2026-11-10.md)). Default `python` until it has run for real (A18).
- **TLS inspection (D78, [ADR-0018](adr/0018-openshell-sandbox-and-tls-inspection.md)).** `[tls_inspection] enabled = true` in an environment, off by default and never set by a preset, so it shows as a privacy warning.
  - **How it works:** Egress ends a run's CONNECT with a certificate from Cairnward's local CA (`<data_dir>/egress-ca/`, EC P-256, 0600; host certificates for seven days, in memory only), opens its own verified TLS connection to the host, and passes HTTP/1.1 through h11 (ALPN offers only `http/1.1`).
  - **Rules:** Each request is checked against `rules` (host, method, path glob; first match wins) and `unmatched`. Rules see the path as a server would resolve it: escapes of unreserved characters decoded (`%2e` is a dot, `%61` is `a`), repeated slashes collapsed, `.` and `..` segments removed (never above the root); the query isn't matched. A path with an encoded slash, backslash or NUL (`%2f`, `%5c`, `%00`, `\`) is refused whatever the rules say.
  - **Every reading of the path:** Because servers differ in how they read a path, each request is checked under every reading: as written, without `;` parameters in any segment (`;` or `%3B` up to the next `/`, as Tomcat, Jetty and Spring drop them, so `/a/..;/b` is `/b`), and both of those ignoring letter case (rule patterns folded too). The first matching rule decides each reading, and the request goes out only if every reading is allowed; a denial names the reading that caused it. So neither case nor parameters can take a request past a deny rule or out of an allow rule. The cost falls on legitimate requests only: an allow rule's path in other letter case, or with `;` parameters that change which rule matches, is refused.
  - **Denials and audit:** The request is forwarded as sent. A denied request gets `403` + `X-Cairnward-Decision: deny` from egress and never reaches the host. Each request is audited as `net.request` with method, status, bytes, the rule, and a digest of the path and query, never the text. Plain-HTTP requests in that environment follow the same rules.
  - **What is and isn't inspected:** `skip_hosts` are tunnelled unread (for clients that pin certificates). The core's own sessions are never inspected, and neither is any program that wasn't given the CA: MCP servers, restic, `mcp login`, plugin commands and downloads share the environment but are tunnelled. Only `cairnward run` marks its session as given the CA (`tls_trust`). A request whose `Host` or absolute URL names another site than the tunnel was opened to is refused (domain fronting, T-34).
  - **The CA:** Runs get a CA bundle through `SSL_CERT_FILE`, `REQUESTS_CA_BUNDLE`, `CURL_CA_BUNDLE`, `GIT_SSL_CAINFO`, `PIP_CERT`, `DENO_CERT`, `AWS_CA_BUNDLE` and, for Node, `NODE_EXTRA_CA_CERTS`. `cairnward egress-ca show|rotate` manage the CA, and a rotation is audited.
  - **Limits:** Request bodies are passed on, not redacted, and WebSocket messages after the upgrade aren't inspected.
- **Environment hygiene:** agents start with a clean environment plus only what their profile needs; `no_proxy`/`NO_PROXY` are cleared inside the sandbox (srt sets them to loopback, which would make clients bypass the proxy and fail to reach the gateway).
- **Allow-list generation:** srt refuses catch-all allow-lists, so Cairnward generates srt's allow-list from the effective policy (gateway address + allowlisted + approved hosts) and updates it live via srt's `--control-fd` when the user approves a host.
- **`cairnward-egress`** (implemented in T0.9): a stdlib asyncio HTTP proxy on loopback. Each connection carries a session token in the proxy credentials (`http://cw:<token>@127.0.0.1:8486`) identifying environment, client and run; no token → `407`. Every `CONNECT host:port` or plain-HTTP request goes through the policy engine (asks time out to deny, `[egress] approval_timeout_s`), is refused with `403` + `X-Cairnward-Decision: deny` and the reason, or is tunnelled and audited with byte counts and duration. Names resolving to loopback, link-local, multicast or cloud-metadata addresses (including IPv4 hidden in IPv6 forms) are refused unless that exact IP is allowed, and the checked address is the one dialled. The token is never forwarded upstream. A plain-HTTP request's `Host` header is set from the checked URL, never taken from the client. Plain HTTP is one request per connection: egress passes that request and its response through h11, tells the client `Connection: close`, and never sends anything the client wrote after the request (a pipelined second request would otherwise reach the host unchecked). A malformed request, such as one with two `Host` headers, gets `400`. Approvals are asked one at a time; a prompt still open after its connection timed out holds the next one back, and a late answer is dropped, not remembered.
- `cairnward doctor` verifies isolation by attempting direct connections from inside each sandbox and expecting failure, independently of srt.
- **Isolated mode (implemented in T0.13).** srt gives the sandbox its own private loopback, so the core can't listen on the host's `127.0.0.1:<gateway_port>` itself. Instead: the core (gateway, management API, vault, policy) runs inside srt and serves **Unix sockets** in the state folder (0700 folder, 0600 sockets); the egress process (the only process with real network access) also runs a byte-for-byte forwarder from `127.0.0.1:<gateway_port>` to the gateway socket. Inside the core, every connection, including to local model servers, goes through srt's proxy (local backend addresses allow-listed; others handed to egress). The core's own egress session is *trusted*: model calls already checked under the right environment are logged and address-checked by egress, not decided twice. The core stops by itself if the egress process disappears. Because the core's sandbox allows Unix sockets, srt's seccomp helper (which also refuses VM sockets) is off there, so the core installs its own filter at start: `socket(AF_VSOCK)`, `io_uring_setup` and foreign-ABI system calls fail with `EPERM`, inherited by everything it starts. Under WSL2 the core refuses to start without it; elsewhere a missing filter is logged (I1, T-28). Approvals inside the isolated core are refused until the dashboard inbox exists (M2); egress prompts on the terminal.
- **`cairnward run`** (T0.12): the agent gets a per-run gateway key and egress session from the management socket (revoked at exit), a clean environment (no inherited tokens or cloud credentials), and an srt sandbox: writes only to the workspace and the agent's state folders, the home folder hidden except what the run needs (§4.3), secrets plus Cairnward's own config/data/state, PaperQA2's cache and the backup and replication folders hidden always, network only to the gateway and hosts allowed for the environment (via egress). `run.start`/`run.end` and every model call carry the run ID.
- **OpenShell as a second sandbox (D77, [ADR-0018](adr/0018-openshell-sandbox-and-tls-inspection.md)).** With `sandbox = "openshell"` (and isolation on), `cairnward run` creates an OpenShell sandbox (`[openshell] image`, default the gateway's) whose policy allows only `host.openshell.internal` (`[openshell] host_name`) on the gateway and egress ports, as plain TCP, for every binary. The agent reaches Cairnward by that name, and egress applies the environment's network rules as for srt. The run's settings go in as an uploaded env file, sourced before the agent starts, so the run key never appears in a command line. The workspace is uploaded beside it, and when the agent exits it is downloaded and mirrored back (new and changed files written, deleted ones deleted). Nothing is written or deleted through a symlinked folder, and a folder is never replaced by a symlink: such paths are listed and the sandbox kept. If the download fails, the workspace is untouched and the sandbox is kept. Cairnward doesn't pin the program (it's in the image), because OpenShell pins executables itself. `home_access` doesn't apply: nothing from the home folder is in the sandbox. Tested against a stand-in CLI only.
- **Unsandboxed programs can still reach local model servers directly** (e.g. Ollama on 127.0.0.1:11434) and bypass Cairnward; client guides configure chat apps to use the gateway and turn off their own Ollama connection.

---

## 4. Components

### 4.1 Model endpoint and backends
- OpenAI-compatible endpoint: `/v1/chat/completions` (streaming, tools), `/v1/embeddings`, `/v1/models`. An Ollama-compatible endpoint (`/api/chat`, `/api/tags`) is a later option for clients that only speak Ollama.
- Backends as in v0.5: named entries in `backends.toml` (`ollama`, `openai_compat` drivers; `location = local | lan | hosted`); hosted denied by default, redacted, ☁-marked, never used for locked conversations; fallback only to local backends.

**Virtual models (D48).** `/v1/models` lists one entry per environment × effort level, e.g.:

| Virtual model | Resolves to |
|---|---|
| `cw/coding:low` | fast model (`qwen3:1.7b`), no thinking, low limits |
| `cw/coding:high` | main model (`qwen3:30b`), thinking on, larger budgets |
| `cw/coding:medium` | main model, thinking on (it can't be turned off on this model) |
| `cw/raw/qwen3:30b` | pass-through to a specific model with the environment `default` |

Picking a virtual model in any client selects effort (thinking mode and budget, sampling, context size, per-request token/time limits) and environment (allowed tools, network policy, redaction, backend). Effort levels and environments remain user-definable TOML (`effort.toml`, `environments/*.toml`; schemas as in v0.5).
- Model defaults (D61) are re-checked against current releases at each milestone (Qwen 3.5 models are now available in Ollama) by re-running the benchmark. Default roles: main `qwen3:30b` (or its successor), fast `qwen3:1.7b`, vision a small Qwen-VL model on demand, embeddings Qwen3-Embedding-0.6B.
- **Cache-aware scheduling (D62).** On CPU, reading the prompt is most of the wait, and re-reading an unchanged conversation start is avoidable:
  - The gateway **never modifies the start of a prompt** (no injected timestamps, no reordering of system prompt or tools); anything it must add goes at the end.
  - **Conversation affinity:** requests from the same conversation are kept together; a request from a different conversation is queued briefly (configurable, default 0 s for interactive clients) rather than evicting a warm cache mid-session.
  - **Keep-alive:** the main model stays loaded while a session is active and is unloaded after an idle period (default 10 min) to give the desktop its memory back.
  - **Cache save/restore:** with the llama.cpp backend, each conversation's KV cache is saved to disk on eviction and restored when it resumes (llama-server slot save/restore), so returning to a long conversation takes seconds instead of minutes. Ollama doesn't expose this; the dashboard recommends llama.cpp for heavy users.
  - **Memory check:** before loading a model, the gateway compares its size plus context against available RAM; if loading would push the system into swap it warns (and, in Balanced/Strict presets, queues instead of loading). `cairnward doctor` reports which combinations of models and context fit.
- **Implemented (T0.10/T0.11):** `cairnward serve` runs the gateway (`/v1/models`, `/v1/chat/completions` with streaming and tool calls, `/health`) and cairnward-egress, in the same process when isolation is off (with isolation on, the core runs sandboxed and egress in a separate process: T0.13, §3.2). Clients authenticate with keys from `cairnward client add <name>` (only a hash is stored). Requests with a non-loopback `Host` header are refused (`421`) and no CORS headers are sent; `Origin` is not checked, so desktop apps that send one still work. The Ollama driver uses Ollama's native API so the context size, thinking switch and keep-alive can be set; the OpenAI-compatible driver sets thinking via `chat_template_kwargs.enable_thinking`. Calls to LAN/hosted backends go through egress with a per-environment session, so egress applies the same environment's rules. The API contract is committed as `api/openapi.json`.
- **Reasoning written into the answer (T-26):** some models reason in their visible reply with thinking off, ending in a stray `</think>`. The gateway moves that text to `reasoning_content`, audits the leak, and asks that model for reasoning from then on (`<state_dir>/reasoning_leaks.json`). A stream can't be taken back, so with thinking off, the first streamed reply of a model not yet seen answering cleanly holds back its answer text. Tool calls, separate reasoning and finish signals still flow. When `</think>` arrives, what came before it goes out as `reasoning_content` and the rest streams live. A reply that ends without one is released whole before its finish, and the model is recorded as clean (`reasoning_clean.json`), so it streams directly from then on (I5).
- **Vault recording:** each message gets a prefix hash over a normalised form (role, content, tool calls), so a client echoing the assistant's reply without `reasoning_content` or with `null` content still continues the same conversation; an edited earlier message continues as a branch from that point.
- **Context reporting:** every response carries standard `usage` counts (shown by most clients), and the dashboard's live view shows context used vs limit per conversation. Expected waiting time is estimated from the stored benchmark profile (new, uncached prompt tokens ÷ measured reading speed); requests expected to take more than 60 s before the first token (≈ 1,300 new tokens on the reference machine's main model) are flagged.

- **Resolution and fallback (implemented in T1.7):** a virtual model resolves its backend and model in this order: the request itself (`cw/raw/<model>`), the effort level's own `model`/`backend`, the client (`backend`, `[clients.<name>.models]` in clients.toml), the environment, the global roles. A backend may name a `fallback` (and `fallback_model`); config validation refuses a fallback that isn't local (FR-BACK-3). When the backend is unreachable or answers 5xx before anything reached the client, the request is retried once on the fallback, recorded as `model.fallback` and in the call's audit record, and flagged in `X-Cairnward-Warning` (non-streaming); client errors (4xx) aren't retried.
- **A run is pinned to its environment:** an environment also selects the privacy level (D57), so an agent must not be able to pick a more open one. A run key from `cairnward run --env X` (or a plugin's key) can only make model calls in `X`, as on the MCP routes: `/v1/chat/completions` and `/v1/embeddings` answer `403` (`run_environment`) to a `cw/<other>:<effort>` and audit it as a denied `model.call` under `X`, with `requested_environment` in the details; nothing reaches a backend. `cw/raw/<model>` uses the run's environment instead of the client's default. `cairnward run` refuses a `--model` from another environment before starting the agent. A client's own key isn't pinned and may name any environment. Listing models isn't filtered: `/v1/models` still shows a run every environment.
- **Disabled clients:** a client with `enabled = false` in clients.toml gets `401` with any of its keys, its run keys included, on chat, embeddings, models and MCP. Its keys start working again when it is switched back on. These refusals aren't audited.

### 4.2 MCP proxy and tools
- Clients connect to one MCP endpoint (streamable HTTP, or `cairnward mcp` as a stdio shim). Cairnward aggregates the MCP servers enabled for the client's environment and applies policy, audit and redaction to every call.
- Third-party MCP servers run in their own sandboxes (§3.2) with only their declared permissions.
- Pinning (v0.5 D38): pinned versions and hashes; tool name/description/schema hashed at approval; any change disables the tool until re-approved; descriptions scanned for instruction-like text.
- Built-in Cairnward tools (exposed via the proxy): `vault.search`, `artifact.write`, `encrypt` (user-only, §5.3).
- Skills: SKILL.md folders are served to clients that support them.
- **Implemented (T2.10):** skills are folders `<config>/skills/<name>/SKILL.md` (front matter `name`, `description`); an environment offers those in its `skills = [...]` as MCP prompts (`prompts/list`, `prompts/get`, audited as `skill.use`) and their other files as read-only resources `skill://<name>/<path>` (hidden files and links out of the folder are not served; scripts are never run). `cairnward__lit_search` (opt-in with `tools = ["research"]`) searches the environment's indexed libraries and returns passages with titles and pages (also as structured content); its output counts as untrusted content for provenance. `cairnward__lit_ask` (same opt-in) answers with page citations (T1.3); `cairnward__lit_review_start` queues a review job (T1.4). `cairnward__lit_find` (environments with `research_sources` and the `research` or `scholar` tool, e.g. `litreview-online`) searches the academic APIs for the model through the same checks as `cairnward research search` (each host as `net.connect`, the query scanned as model-initiated outgoing text, audited by digest); results are untrusted content; adding papers stays with the user (`research add`). **Remote MCP servers** (`url` in `mcp.toml`, streamable HTTP through egress): see the ADR-0015 amendment.
- **Implemented (M2, 2026-11-01, [ADR-0015](adr/0015-mcp-proxy.md)):** `mcp.toml` declares stdio servers with a permission manifest (hosts through egress, writable folders inside the workspace or the server's own folder, optional readable folders, passed-through variables), per-tool `allow / ask / deny`, and optional `pin_file` + `sha256`. Environments list `mcp_servers`. The gateway serves `/mcp/<environment>` (streamable HTTP, JSON responses, per-client sessions, Origin check); `cairnward mcp` is the stdio bridge; `cairnward run` passes the endpoint to agents (opencode's per-run config includes it). Tools appear as `<server>__<tool>`; each call is a `tool.call` action (policy, approvals, audit pre/post with argument and result digests). Pinning hashes whole definitions and offers only approved ones (`cairnward mcp tools | approve`); changes are audited (`mcp.tool_changed`) and disable the tool; descriptions are scanned for instruction-like text. Servers start on first use in their own sandbox; with the core isolated the outer process starts them (srt can't nest) and hands the core a socket. Not yet: remote MCP servers, built-in `lit.*`/`vault.*` tools on this endpoint, redaction of tool traffic, idle shutdown.
- **OAuth for remote MCP servers:** `auth = "oauth"` in `mcp.toml`. `cairnward mcp login <server>` signs in once, in a terminal, on the MCP SDK's client: discovery, dynamic registration and PKCE, the answer coming back to a one-time listener on 127.0.0.1, and the login's requests going through an egress session in the environment. Tokens are kept owner-only in `<state>/mcp-oauth/<server>.json`, never in config or the audit log; `mcp logout` forgets them. The core only uses and refreshes tokens and never starts a sign-in: when a token can't be refreshed, connecting fails with a message to log in again. Every host it talks to, the sign-in server's included, is checked by policy on every request. Tested with the real SDK against a fake authorization server on real sockets; no real provider tried yet.

### 4.3 Coding agents through the gateway (D43)
- `cairnward run [--env coding] -- <agent> [args]` starts any agent in the sandbox with its model base URL, API key and MCP config pointed at Cairnward. Ready-made profiles for **Qwen Code, opencode, Aider and Goose** (all four tested); any OpenAI-compatible agent works with a generic profile.
- Approvals for edits and commands stay inside the agent (each has its own). Cairnward adds:
  - sandboxing (filesystem and network),
  - an audit of every model and tool call, linked to the run,
  - **workspace snapshots:** before and after each run, Cairnward commits the workspace state to a hidden ref (`refs/cairnward/runs/<id>`), so every change an agent made is reviewable and revertible (`cairnward runs diff <id>`, `cairnward runs revert <id>`),
  - per-run budgets (tokens, time, requests) enforced at the model endpoint.

**Implemented (coding work, 2026-11-01):**
- **What a run can read (`home_access`, §5.0):** with `hidden`, the run's sandbox denies the home folder and re-allows only: the workspace and run folder; the profile's state folders; git config (`~/.gitconfig`, `~/.config/git`) and the profile's settings files; language runtimes (uv's Pythons, fnm, nvm, pyenv versions, mise, asdf, Volta, rustup); srt's package; and the program itself, found by following links and `#!` interpreters from the command, plus the active venv, the script an interpreter runs, and folders on `PATH`.
  - **What stays hidden:** A program's whole prefix is re-allowed only when it has `lib/` or `pyvenv.cfg`, never the home folder or `~/.local` as a whole. The list of known secret folders and Cairnward's own folders stay hidden inside all of that. Writes elsewhere under the hidden home folder succeed in a throwaway tmpfs and never reach the disk.
  - **Fallbacks:** If the workspace contains the home folder, hiding it can't apply: the run warns and uses the secrets list. `home_access = "readable"` (per environment) goes back to that list too, with a warning at each run; like any setting it shows as weaker than Strict and a change is audited. A program this search can't follow (pyenv shims, an install that loads files beyond its own folder) won't start; use `readable` for that environment.
  - **Aider:** `~/.env` isn't re-allowed for Aider.
  - Tested in the real sandbox with a fake home holding planted secrets and against the real home, and with real Aider on an isolated server and a scripted model. Qwen Code's `npm install -g` layout was tested with a stand-in only.
- **Snapshots:** before and after every run the workspace is committed into a shadow git store in the state folder (one per workspace; the project's own `.git` is never touched; `.gitignore` honoured; dependency/build folders and files over 20 MB left out). `cairnward runs list | diff <id> | revert <id> [--force] | prune`; revert skips files changed again since the run unless forced, takes a safety snapshot first, and is audited (`run.revert`).
- **Profiles, tested with the real agents** in the sandbox under Strict against a scripted model: Aider 0.86.2 (diff edit format by default, analytics off), Qwen Code 0.26.5, opencode 1.19.5 (per-run config file, removed with the run). Goose's profile is untested. Observed and blocked by the sandbox: Qwen Code's usage telemetry (`*.rum.aliyuncs.com`) and model catalogue (`models.dev`), opencode's provider download (`registry.npmjs.org`), LiteLLM's price-list download inside Aider (`raw.githubusercontent.com`); all three agents keep working without them.
- **Budgets:** `--max-requests/--max-tokens/--max-minutes` or `[run_budget]` per environment; exceeding returns `429 budget_exceeded` and is audited. A client can also have a daily limit in `clients.toml`, `[clients.<name>.budget]` with `max_requests_per_day` and `max_tokens_per_day` (unset = unlimited; embeddings count as requests only, and a client's runs count towards it). Same response and audit record. The counts are kept in `<state_dir>/client_usage.json`, so a restart doesn't reset them; the day is the machine's local calendar day. Limits are checked before a request, so the request that crosses a token limit finishes, and requests running at once can pass a limit slightly.
- **Scheduling (D62):** one queue per backend (`max_parallel`, default 1); the conversation whose prefix the model has cached goes first, at most 3 times in a row. The gateway estimates each request's uncached prompt tokens from the previous request on that backend; with speeds imported by `cairnward bench import`, requests expected to need more than 60 s before the first word carry an `X-Cairnward-Warning` header and audit note. Prompts larger than the context warn, or are refused with `context_overflow = "reject"`.
- **Evaluations:** `evals/coding/run_eval.py` runs an agent on a task repository through `cairnward run` and reports pass/fail, time, model calls and tokens from the audit log.
- **Prompt sizes (FR-AGT-1, T2.9):** at the end of each run the first model call's prompt size is recorded per profile (`<state>/prompt_sizes.json`); `cairnward run` warns before starting when the last measured size exceeds the virtual model's context or, with an imported benchmark, would take more than 60 s before the first answer.
- `CAIRNWARD_SANDBOX_DEBUG=1` makes the sandbox log every allowed and blocked connection, to see what an agent tries to reach.
- **Approvals during runs (M2, CLI inbox):** every "ask" (from the gateway, egress or a sandboxed run) waits in the core's approvals inbox, answered with `cairnward approvals` (watch mode with y / c / e / n, `list [--json]`, `answer <id> <reply>`), with a desktop notification through `notify-send` when available; no answer within `approval_timeout_s` is a no. In isolated mode the egress process asks the core's inbox over the management socket. With `network = "ask"` (or `"open"`), `cairnward run` starts srt through a small Node wrapper around srt's library (`sandbox/srt_ask.mjs`) whose ask callback sends hosts not on the sandbox's list to the core: the policy engine decides for the run's environment, an approved host stays allowed for the rest of the run (in the core and in egress), and every grant is audited (`approval`). srt connects to loopback destinations directly rather than through egress, so the core applies egress's address guard when the sandbox asks: this machine's addresses (including `localhost`, inet_aton shorthand such as `127.1`) and link-local/metadata addresses are refused unless listed exactly. Needs srt installed as the node package (`cairnward sandbox install`); otherwise unlisted hosts stay blocked with a warning. The dashboard inbox, batching of similar requests and persisted grants come with the dashboard.

### 4.4 Chat clients (D44)
- Documented and tested: **Open WebUI** (check its current license's branding terms), **Jan**, and one more chosen at M2 (LibreChat or AnythingLLM). Each gets a setup guide: base URL, per-client API key, MCP endpoint, recommended privacy settings (telemetry off, history minimal or temporary chats, since the vault holds the authoritative copy).
- Features like chat folders, uploads and artifacts panels come from the client. Effort/environment come from virtual models (§4.1).
- **Implemented (T2.9):** LibreChat chosen as the third client (server-style, MIT, custom endpoints and MCP). Container templates for Open WebUI and LibreChat put the client on an internal-only network whose only way out is Cairnward's **client socket** (`<state>/clients/gateway.sock`, created by `serve` in both modes, owner-only, in its own folder), joined to `127.0.0.1:8484` inside the client's network namespace by a socat sidecar; the UI is published on 127.0.0.1 through a relay; image versions must be pinned in `.env`. Checked with `docker compose config`; hands-on checks with the real apps are in `docs/clients/checklist.md` (owner-side).

### 4.5 Transcript vault (D47, [ADR-0011](adr/0011-transcript-vault.md))
- Every request/response through the model endpoint is stored. Conversations are reconstructed from the OpenAI API's stateless requests by **prefix hashing**: each request carries the full history, so a request whose message prefix matches a stored conversation extends it; a different prefix starts a new conversation or a branch.
- Records are encrypted per conversation with age keys (crypto-shredding; v0.5 §5.5). Conversations can be **locked** with a passphrase (D23/D24), the same as v0.5's chats. **Optional recovery key (FR-ENC-1):** `cairnward vault recovery-key create` makes an age X25519 identity, shows its secret once and stores only the public half (keys.db); from then on each lock also encrypts the conversation key to it, and conversations locked earlier are added the next time they're unlocked (the key is in memory then). `cairnward vault recover <id>` takes the recovery key and sets a new passphrase (still locked; audited as `vault.recover`); `recovery-key status` says how many locked conversations it covers; `recovery-key remove` destroys every copy. The secret never reaches the audit log, the database files or the server (tested). Anyone holding it plus the data folder can open the covered conversations, so it belongs off the machine.
- **Vault browser** in the dashboard: search (FTS), folders, pin, lock, export, delete, with the keyboard shortcuts from v0.5 (`j/k`, `/`, `d` delete with confirm and undo, `f` move to folder, `L` lock, `x` egress report, `l` audit, `?` help; single-key shortcuts can be turned off).
- Deleting in the vault does **not** delete the copy in the client's own history; the dashboard says so, and setup guides show how to minimise client-side history.
- Vault retention per environment (`keep forever` default; or N days); `vault = "off"` per environment stores only audit digests.
- **Implemented (T2.4, CLI):** `cairnward vault list | search | show | export | delete | restore | purge | pin | title | prune`, with id prefixes. Search decrypts as it goes instead of keeping a plaintext full-text index (fine for personal vaults; revisit if it gets slow). Exports are plain text files (0600) and audited, as are deletes, restores and purges. Retention (`vault_retention_days`) purges an environment's conversations untouched for N days, keeping pinned ones; the trash empties after 30 days; both run at start and daily in the core and on `vault prune`, recorded as `vault.retention`. `vault = "digests"` and `"off"` store nothing in the vault (the audit log keeps digests).

### 4.6 Plugins (D75, [ADR-0017](adr/0017-research-as-a-plugin.md), [guide](plugins.md))

A plugin is an MCP server with `kind = "plugin"`. It gets no capability that a protection depends on: it asks a core service, the service enforces the protection, and the sandbox stops it going around the services.

- **Sandbox required**, whatever the environment's `isolation`, with `read` required and `write` only inside `{data}`. `unsandboxed = true` is the one way out: warned about, on the dashboard and audited.
- **No network of its own:** only the gateway. `permissions.network` may list exact local addresses, such as Zotero's `127.0.0.1:23119`, shown at install and audited. An outside host isn't allowed there, which is narrower than the ADR (below).
- **Services**, with a per-launch key that's held in memory, revoked at stop and pinned to the environment: the gateway for models; `POST /v1/cairnward/fetch` for outside requests (`net.connect` check, redaction, through egress, audited as a digest; hosts limited to `fetch_hosts`; 2 MB out, 64 MB back, a rate limit, four at a time; redirects are returned, not followed); `/v1/cairnward/artifacts`, which reaches only `plugin/<name>`; `/v1/cairnward/item-keys`, for a key stored with the conversations' keys; and `/v1/cairnward/model-info`.
- **Pinned code and tools:** `pin_dir` must match `sha256`, or it doesn't start. Changed tool definitions are held for review; only `unpinned_tools` turns that off.
- **Output is untrusted:** a tool on another server, or Cairnward's own artifact tools, called after a plugin's output and before the user's next message needs approval, naming the plugin, and that approval is never remembered. Not covered: an agent's own network requests after reading plugin output, which egress can't link to it.
- **Conformance:** `cairnward plugin verify <name>` starts a probe with the plugin's exact sandbox and key against a running core, then the plugin itself. A plugin that hasn't passed for its current code and contract version doesn't start.
- **A plugin's key can't use the MCP endpoint,** so it can't call other plugins' tools.
- **Versioned:** `contract_min` and `contract_max` must include the core's contract version (now 1).
- **Commands:** `cairnward plugin add | list | pin | verify | run | allow-read | helper | remove`. `plugin run` starts a command in the plugin's sandbox with your terminal. `plugin helper` starts a program that can't use fetch (rclone) outside the sandbox with its traffic through egress.

Literature research is one such plugin, a separate project (`literature-review-agent-plugin`, package `cairnward-research`) with its own design, requirements, threat model, status and evals. The core knows nothing about it beyond two kinds of leftovers that keep an older setup working: commands that moved print where they went, and old settings (`[research]`, `research_sources`, `include_research`, `citation_style`) still load, with all but `include_research` ignored. A plugin brings its own environments if it needs them (D76): the core ships only `coding` and `scratch`.

### 4.7 Artifacts
Git repo per conversation, plugin job and agent run (one commit per write; restore = new commit). Previews of HTML/SVG in the dashboard use a sandboxed origin with no network.
- **Implemented (T1.9):** repositories in `<data>/artifacts/<conversation|job|run>/<id>/` (owner-only); a write that changes nothing makes no commit; restore writes the old content as a new commit; paths are confined to the repository (no `..`, absolute paths or `.git`), 2 MB per file. Conversations' artifacts are deleted when the conversation is purged; research job results are committed to the job's repository.
  - **Access:** Models write through MCP tools (`cairnward__artifact_write | read | list`, opt-in with `tools = ["artifacts"]`, policy-checked and audited, scoped to the client's current conversation). Agent runs keep using workspace snapshots (`cairnward runs`). `cairnward artifacts list | files | log | show | restore` and the dashboard's Artifacts view (versions, restore).
  - **Previews (FR-ART-4):** HTML and SVG open in an `<iframe sandbox>` served with `Content-Security-Policy: sandbox; default-src 'none'; style-src 'unsafe-inline'; img-src data:` (opaque origin, no scripts, no network); a browser test checks that a preview's script doesn't run and its external image isn't fetched.
  - **Locking (FR-ENC-1):** locking a conversation seals its whole artifact repository, history and file names included, into one file (`sealed.age`) encrypted with the conversation's own age key, so its passphrase, recovery key and crypto-shredding all apply. While it's locked nothing can read or write its artifacts; `cairnward artifacts` and the dashboard show it as locked. While it's unlocked in the server, each operation works on a temporary copy (in `$XDG_RUNTIME_DIR` when there is one, else the system's temporary folder) that's deleted afterwards, and a write seals the repository again. Removing the lock restores the plain repository. A repository under a conversation locked before this existed is closed at once and sealed at its next unlock.
  - Limits: earlier plaintext isn't erased from the disk's free space or from old backups (`purge` removes it from backups).
  - **Jobs and runs:** `cairnward artifacts lock job|run <id>` locks a finished job (its artifact repository and its folder, leaving a stub with only its id, kind, status and times) or a run's artifact repository, with a new key under its own passphrase, covered by the recovery key; `unlock` removes the lock and restores the files. Run snapshots (`cairnward runs`) are kept per workspace, not per run, and can't be locked.

### 4.8 Sync and backup (D27, D30, D40, D50)
As v0.5 ([ADR-0007](adr/0007-sync-restic-backup-and-event-log-replication.md)), now covering the **vault, plugins' data and artifacts**:
- Modes `none` (default), `on_demand`, `auto` (on change and/or schedule) for every job.
- `backup` jobs: restic (always encrypted), retention policies.
- **Purge rewrites backups (D50):** purging a conversation runs `restic rewrite --exclude` for its records and key across all snapshots, then `restic prune`. Until that completes the dashboard shows "pending removal from N backups"; failures are retried and reported.
- **Replication implemented (FR-SYNC-4/6, D72):** `cairnward replicate init <folder>` on the first device shows a sync key once; `replicate join <folder>` on another asks for it, then `replicate approve <id>` on an existing device accepts its changes. `[replication] mode = "none" | "on_demand" | "auto"` (auto: `serve` syncs every `interval_minutes`, in isolated mode from the outer process; a restart picks up a change), `exclude_environments` keeps an environment's conversations on the device, `include_artifacts`.
  - **What is shared:** Conversations, their messages, keys and artifacts, and folders, are shared. Research libraries, jobs, settings and the audit log are not.
  - **Format and crypto:** Each device writes only its own folder of files, so nothing can conflict at the file level. Every record is encrypted with the sync key (ChaCha20-Poly1305), signed by its device (Ed25519; a device's id is the hash of its public key) and bound to its device, file and position; message bodies stay encrypted with the conversation's key underneath, so "encrypt this sync target" is always on and a locked conversation can't be read with the sync key.
  - Merge: messages are a set ordered by (Lamport clock, device); two replies to one message are a fork in the message tree, shown as a fork by `vault show` and the dashboard; conversation fields are per-field last writer wins; a purge always wins and each device deletes its own files for that conversation when it next syncs; a lock or unlock on one device follows on the others, with artifacts sealed or unsealed to match, and the older copy of the key is deleted from the folder.
  - **Artifacts:** Artifacts are git bundles merged the same on every device (both sides changed a file: one stays at its path, the other is kept beside it as `<path>.from-<commit>`); for a locked conversation they're one sealed file and the later write wins.
  - **Damaged or partial folders:** A missing folder (not mounted) is refused without writing, a file that got shorter or a record that fails its checks is reported and reading that file stops there, and a record that needs another file not yet arrived waits for it.
  - **Limits:** a device that hasn't synced still holds a purged or newly locked conversation, and its files stay in the folder until it does; the folder's own version history (Syncthing versioning, cloud drives) is outside Cairnward's reach, so turn it off for this folder; the folder's holder sees device and conversation counts, sizes and times; a recovery key is per device.
  - **Blocking:** Blocking stops a device's changes being accepted, but it keeps the sync key and can still read the folder: shutting it out means a new folder (`leave`, then `init`, and `join` from the others).
  - Tested in `tests/test_replication.py` with separate devices' folders on one machine, including 3-device random changes and syncs ending identical (Hypothesis), files delivered one by one in random order, tampering and crashes. **Not tested between two machines or through real Syncthing or a NAS.**
- Drive mirror (incoming) via rclone, read-only, with preview.
- **Backups implemented (T3.3):** `[backup] mode = "none" | "on_demand" | "auto"`, `repository` = a local folder (second disk, NAS, Syncthing folder, or a cloud drive mounted locally) or a remote restic backend that speaks HTTP (`s3:`, `rest:`, `b2:`, `azure:`, `gs:`, `swift:`, `rclone:`; `sftp:` is refused because SSH wouldn't go through egress).
  - **Staging (I40):** the export is built in memory (`$XDG_RUNTIME_DIR/cairnward-backup-staging`, a fixed name so snapshots stay incremental) and removed when the run ends, failed or not. It goes on disk (`<state>/backup/staging`) only without a memory folder (warned) or with `[backup] staging = "disk"`, for exports larger than memory allows.
  - **Remote repositories:** For a remote, restic runs inside an egress session in `[backup] environment` (default: the default environment), so its network policy applies and its traffic is in the egress report under the client `restic`; `serve` must be running.
  - **Credentials and settings:** Credentials for the backend come from restic's own environment variables, never from Cairnward's config, `interval_hours`, `keep_daily/weekly/monthly`, optional `password_command` (default: a generated password in the state folder, shown once at `cairnward backup init`).
  - **What a snapshot holds, and purges:** A snapshot holds a **per-conversation export** (one folder per conversation with its rows exactly as stored, ciphertext when encrypted, and its key, wrapped when locked) plus research.db, the audit log, artifacts, jobs, MCP pins and the config folder, so a purge can be removed from every snapshot: the vault queues each purge, and the next run (or within the hour under `serve`) does `restic rewrite --forget --exclude conversations/<id> --exclude artifacts/conversation/<id>` and `restic prune`, then checks with `restic find` that no snapshot still holds it; until then it shows as pending (`cairnward backup status`, the doctor, the dashboard's vault view) and is retried.
  - **Privacy of snapshots:** Snapshots record the host as "cairnward", not the machine name; for a folder repository restic runs with no network settings.
  - **Restore:** `cairnward backup restore [--at DATE | --snapshot ID]` brings back conversations the vault doesn't have (never overwriting, purged ones stay purged); `--to DIR` only extracts.
  - **Isolated mode:** In isolated mode the outer process runs restic (the sandboxed core can't write to the backup folder).
  - Tested: purged content absent from all snapshots afterwards, restore after losing the vault, purge pending while the disk is unplugged.
- **Where backups go:** with no `repository`, `~/cairnward-backups`. `cairnward backup use [folder] [--mode] [--access owner|group]` turns backups on and sets up the repository. `[backup] access` sets who can read a folder repository, owner-only by default, and is reapplied after each backup because restic creates new read-only files. A folder on the same disk as the data gets a warning in the CLI, `doctor`, `status` and the dashboard.
- **Dashboard settings in isolated mode:** the core can't write the configuration folder, so it sends privacy and backup edits to the outer process over the control socket (`POST /v1/settings/{kind}`), which applies the same checks as anywhere (T-32).
- **From the dashboard:** the Backups view has **Back up now**, which sets up the repository the first time (in isolated mode the outer process runs it, because the core can't reach the folder), and **Change settings** for the mode and a folder. A remote repository stays a change made in `cairnward.toml`. Settings take effect on restart. Both are audited (`backup.requested`, `backup.settings`: which fields changed, not the path).

### 4.9 Dashboard
Loopback web app (hardened as §3.1), the only UI Cairnward builds: **approvals inbox**, live activity, audit browser (per client/run/conversation), egress report, vault browser, artifacts, sync status, settings (backends, environments, effort, clients, MCP servers). Plus a terminal equivalent for key flows: `cairnward approvals`, `cairnward audit`, `cairnward vault`. Accessibility target WCAG 2.2 AA.

**Implemented (T2.7):** `cairnward dashboard` asks the running core for a single-use sign-in link (2 minutes) and opens it; the link becomes an HttpOnly, SameSite=Strict session cookie and leaves the address bar.
- **Serving and security:** Served on 127.0.0.1:`dashboard_port` (in isolated mode the core serves a Unix socket and the outer process forwards the port). Host allowlist, CSRF token header plus Origin check on every change, strict CSP, no CORS, `no-store` on the API.
- Views: approvals (live; provenance warning; passphrase dialog for user-only actions; "yes to all like it"; late answers), activity (live audit tail), audit (filters, chain check, JSON-lines export), egress (filters, CSV), vault (search, trash, pin, folders, lock/unlock dialogs, export, delete with undo; shortcuts `j k Enter / d f L p x l ?`, switchable in Settings), MCP tools (permissions, review and approve definitions with warnings), settings (read-only privacy per environment with weaker-than-Strict values highlighted, backends, roles, clients).
- Tested with request-level security tests and a Chromium end-to-end test (sign-in, live approval, vault shortcuts, an XSS probe, locking, no CSP violations, labelled controls, skip link).
- Since added (2026-11-02): research jobs and artifacts views; a **Backups** view (mode, where (a remote's host only), its egress environment, retention, last backup, purged conversations still in older backups with retries and errors); **editing privacy settings** in Settings: the preset and each enumerated setting globally or per environment (not allow-lists or backends), written to the same TOML file a person edits (comments kept; a built-in environment is written out with its settings first), only if the whole configuration still loads; a change that leaves a setting weaker than Strict asks first (the server answers 409 until confirmed); recorded as `settings.changed` by "user (dashboard)"; takes effect on restart, shown as "saved, not yet in effect" until then (if the process serving the dashboard can't write the config folder, as may be the case for the sandboxed core in isolated mode, nothing is changed and the dashboard says to edit the file; not yet tried with the sandbox, which isn't installed in the build environment).
- Settings also shows the vault recovery key's status.
- **Accessibility:** The browser test now runs **axe-core 4.14.0** (WCAG 2.0/2.1/2.2 A and AA plus best practices) on all ten views in light and dark mode and requires zero violations; dialogs and the sandboxed preview's contents aren't audited.

---

## 5. Security and privacy

### 5.0 Privacy controls and presets (D55–D58, [ADR-0012](adr/0012-optional-privacy-controls-and-presets.md))
Every protection in this section and in §3.2/§4.5/§4.8 is a setting. A **preset** sets them all at once; individual settings can then be changed (the preset shows as *Custom*).

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

- **Open** keeps the audit log on because it costs nothing in daily use; it can still be switched off. With every setting off, Cairnward is a plain router: virtual models, backends and the dashboard's model views keep working.
- **Opt-in features stay opt-in in every preset:** locking a conversation with a passphrase, encrypting a sync target, TLS interception. Presets never lock or encrypt anything on their own.
- **Not switchable** (they are functional, not protective): the passphrase dialog when you lock/unlock something; the local-only binding and Host/Origin checks of the dashboard and endpoints (§3.1), because turning them off would expose Cairnward to any web page or network peer rather than reduce friction. *(If you want these switchable too, say so; see §12.)*

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
Resolution: environment setting → environment preset → global setting → global preset. Because virtual models carry the environment (`cw/scratch:low`, `cw/thesis-confidential:high`), picking a model in any client also picks its privacy level. Coding-agent runs use the environment given to `cairnward run --env`.

**First run:** `cairnward setup` asks for a preset (with a one-line description of each and what it needs installed: e.g. Strict needs bubblewrap and a private git repo for anchors), then checks prerequisites with `cairnward doctor`. Missing prerequisites for a chosen setting are reported, and the setting shows as *requested but inactive* until fixed.

**Visibility and logging of weakening (D58)**
- `cairnward privacy show [--env NAME]` prints the effective value of every setting, where it came from (preset, global, environment), and which settings are weaker than Strict.
- The dashboard header and `cairnward status` show a badge per environment: `Strict`, `Balanced`, `Open`, or `Custom (3 weaker than Strict)`; settings that are off or inactive are listed on hover/expansion.
- Every settings change is an audit record (`settings.changed`, before → after, who/where). Switching `audit` off writes a final `audit.disabled` record first; switching it back on writes `audit.enabled` with the gap's duration, so gaps are visible in the chain.
- Dependencies are explained where a combination weakens a setting silently otherwise, e.g. `network = "allowlist"` with `isolation = "off"` shows *advisory only: tools can bypass the proxy*; `audit_anchor = "git"` requires `audit = "on"`; redaction only covers traffic Cairnward terminates.

### 5.1 Threat model
See [threat-model.md](threat-model.md) (updated for v0.6). Key changes: third-party clients and agents are now untrusted (boundary B7), enforcement moved to the OS, content redaction is limited to traffic Cairnward terminates, and audit checkpoints are anchored externally.

### 5.2 Audit log (D8, D37, D49)
- Hash-chained records (actor = client/agent/run/user, action, target, decision, digests); content lives in the vault, referenced by digest.
- **External anchoring (D49):** every hour (configurable) and on demand, Cairnward commits the latest checkpoint (sequence number + hash + signature; no content) to a **private git repo the user owns** (e.g. `github.com/<you>/cairnward-anchors`), using a deploy key with write access to that repo only. `cairnward audit verify --anchors` checks the local log against the anchors. Honest limit: someone with full control of the machine can still alter records **newer** than the last anchor, or stop anchoring; the gap is visible as missing anchors.
- **Implemented (T2.3):** `cairnward audit anchor --setup` makes two OpenSSH Ed25519 keys (a deploy key to push, a signing key) and explains the repo setup; checkpoints `{seq, hash, time, installation}` are signed with `ssh-keygen -Y sign` and committed as `anchors/<installation>/checkpoints.jsonl` next to the public `signers` file. `serve` anchors every `[anchor] interval_minutes` (60) when `audit_anchor = "git"`, skipping when nothing is new; each attempt is audited (`audit.anchor`). `cairnward audit verify --anchors` fetches the repo and checks signatures, that each anchored record still has its anchored hash, and how many records came after the last anchor; `doctor` warns when anchoring is on but not set up or failing. Needs git and openssh-client.
- Optional OpenTelemetry GenAI export (local).
- **Independent verifier (D81):** `cairnward-audit-rs verify FILE` recomputes every record's hash, the chain and sequence numbers from a JSON-lines export (`cairnward audit log --json --last N`, or `audit.jsonl` when the database isn't encrypted). `root FILE SIZE` gives the Merkle root over the first SIZE records, and `check-proof FILE` checks a proof from `audit prove` (the signature is left to `ssh-keygen -Y verify`). It's written apart from the Python writer, so a mistake in either shows up as a disagreement. Tested against generated records.
- **Merkle checkpoints (D79, implemented 2026-11-10):** each checkpoint (`"v": 2`) also signs `root`, the RFC 9162 Merkle root (as in Certificate Transparency) over records 1..seq: leaves are the records' own hashes, with SHA-256 and the 0x00/0x01 leaf and node prefixes. `audit verify --anchors` recomputes every root from the log and checks, with a consistency proof, that each checkpoint's tree extends the previous one; version 1 checkpoints (hash only) still verify. `cairnward audit prove <seq> [--out FILE]` writes a self-contained proof: the record (metadata and digests only), its audit path (about log2(n) hashes) and the signed checkpoint. `cairnward audit check-proof FILE --signers FILE` checks it without the log or any configuration: the record's hash, its inclusion, and the signature against a signers file the checker trusts (without `--signers` it exits 2, signature unchecked). Lets you show one record to an auditor without handing over the log. Tested: the tree against the RFC 6962 reference roots, every proof for trees up to 70 leaves (Hypothesis) including tampered, moved, short and long proofs, and anchoring end to end with a local git repository and real `ssh-keygen` signatures. Not changed: records newer than the last checkpoint are still unprotected (T-03).

### 5.3 Policy and approvals (D46, D54)
- `allow | ask | deny` per action type, environment and client.
- **Approvals inbox:** "ask" decisions appear in the dashboard, as a desktop notification, and in `cairnward approvals`. The request waits (clients may time out on long waits; the inbox shows "client gave up" and the approval applies to the retry).
- **Avoiding approval fatigue (D54):** actions are risk-tiered: *low* (reads in workspace, GET to an already-approved academic API) → allowed and logged; *medium* (new host, write outside artifacts) → ask once per host/scope with "remember for this environment"; *high* (user-only actions, hosted backend, sending detected secrets) → always ask, never remembered. Similar pending approvals are batched into one prompt.
- **How "ask" interacts with `approvals`** (implemented in T0.8): settings that explicitly say *ask* (`network = "ask"`, `hosted_backends = "ask"`) always ask, whatever the approvals level, because that is what the user chose. Risk-tier asks follow `approvals` (`off` = allow, `high_only` = ask for high risk, `tiered` = ask for medium and high). When nothing can ask the user (no terminal, no dashboard), the answer is **deny**. `cairnward policy explain KIND TARGET [--env E]` shows a decision and its reason without running anything.
- **User-only actions started by the model always ask, even with `approvals = "off"`**, and are never remembered, so prompt injection can't switch protections off unseen. Started directly by the user, they follow the `approvals` setting.
- **User-only actions (D46):** `encrypt`/`decrypt`, sync job changes, enabling hosted backends, adding MCP servers, unsandboxed runs. They are agent tools, but each call opens a **confirmation dialog in Cairnward's own UI** (never in the client), showing the action, the conversation, and its **provenance**: the content that entered the context since your last message (e.g. "after reading `paper.pdf` page 4" or "after web page example.com"). If anything untrusted came after your last message, the dialog says so prominently. Passphrases are typed only in this dialog.
- **Implemented (T2.6, CLI):** the provenance tracker notes tool results after the user's last message (from chat requests) and tool outputs relayed by the MCP proxy, per client and run; model-started user-only actions carry it into the confirmation, which says prominently that untrusted content came first. First user-only tool: `cairnward__lock_conversation` (opt-in with `tools = ["vault"]` in an environment), which locks the client's current conversation after confirmation; the passphrase is typed in `cairnward approvals` (twice, hidden) and passes to the vault without being stored, logged or audited. Similar pending requests can be answered together (`a`). A request whose client stopped waiting stays listed for 10 minutes as "gave up"; answering yes approves the client's next identical request once, so retries don't ask again (FR-POL-5).

### 5.4 Redaction
- Applies to traffic Cairnward terminates: model calls to non-local backends, MCP tool calls, plugins' fetches.
- Built-in light scanner (secret patterns, emails, phone numbers, ID-like numbers, custom regexes) in the core; **Presidio** as an optional worker for richer personal-data detection (D53).
- Defaults: secrets **block**, personal data **ask** (batched; see §5.3).
- **Implemented (T2.1):** built-in patterns for secrets (private keys, AWS/GitHub/OpenAI/Anthropic/Slack/Google/Stripe/Hugging Face tokens, JWTs, Cairnward keys, password assignments) and personal data (emails, phone numbers, payment cards with Luhn check, IBANs with checksum, US SSNs, NRIC-style ID numbers), plus `[redaction] custom` patterns and `ignore` rules in cairnward.toml. Scanned: messages and embedding inputs to non-local backends, MCP tool arguments. `warn`: sent, flagged in `X-Cairnward-Warning`, audited; `enforce`: secrets refused (`403 redaction_blocked`, or a tool error), personal data asks (`data.send`, rememberable per conversation or environment). Audit records hold kinds and counts, never the matched text; content is never altered. Presidio stays optional (later).

### 5.5 Encryption and deletion
Implemented in T0.5: conversations live in `vault.db`, their keys in a separate small `keys.db` (both SQLite, WAL, `synchronous=FULL`, `secure_delete=ON`); purging deletes the key first, then the content, then checkpoints both files, leaving a tombstone row. With `crypto_shred = "off"` content is stored as plain text. Deleted keys are overwritten inside the database files; whether the SSD still holds old blocks is outside SQLite's control, so locked conversations or full-disk encryption remain the hard guarantee. As v0.5 ([ADR-0006](adr/0006-encryption-age-key-wrapping-crypto-shredding.md)): age, per-conversation keys, opt-in passphrase locks (conversations and artifacts separately), crypto-shredding on purge, plus backup rewrite (D50). Stated limits: unlocked conversations' search index is plaintext in SQLite; client-side copies are outside Cairnward's control.

**Implemented (T2.5, locks):** `cairnward vault lock <id>` wraps the conversation's key with a passphrase (age scrypt; at least 8 characters) and overwrites the plain key in keys.db. A locked conversation's title and messages can't be read, it isn't searched, and it is **never sent to hosted backends, even while unlocked** (`403 locked_conversation`). `vault unlock <id> [--minutes 30]` opens it in the running core, in memory only, so new exchanges are stored again; `vault relock` closes them all; `vault show/export` ask for the passphrase in the terminal; `unlock --permanently` removes the lock. While locked, a local model still answers but the exchange isn't stored and the client gets an `X-Cairnward-Warning`. Passphrases are read with `getpass` or arrive on the owner-only management socket; they are never stored, logged or audited (tested by scanning every file Cairnward writes). A forgotten passphrase can't be recovered: an optional recovery key is not built yet.

**Database encryption (D80, [ADR-0019](adr/0019-sqlcipher-encrypt-everything.md), implemented 2026-11-10):** `cairnward store encrypt` (with `serve` stopped) turns on SQLCipher for `vault.db`, `keys.db` and `audit.db`.
- **What it protects:** every page of the three files is encrypted with one random 256-bit key, so titles, folders, timestamps, ids, sizes, the audit records and the unlocked conversations' keys are no longer readable from the files. Per-conversation keys and crypto-shredding stay as above, inside it.
- **The key:** `<data_dir>/db-key.age`, the key wrapped with a passphrase (age scrypt, at least 8 characters).
  - **At start:** `serve` and every command that opens the databases ask for the passphrase on a terminal.
  - **Unattended starts:** `[storage] key = "env:NAME" | "keyring:NAME"` in cairnward.toml may hold the key itself (64 hex characters). This is a weakening: a warning from `serve`, `doctor` and the dashboard, recorded in settings snapshots.
  - **Isolated mode:** the outer process writes the key to the sandboxed core's standard input, read once at start. It's never in an environment, which `/proc/<pid>/environ` would keep (review 2026-11-10, R1). An `env:` reference is removed from the environment once read, so programs started afterwards don't inherit it (R2). Agents, MCP servers and plugins never receive the key.
- **Crash safety:** the wrapped key is saved as `db-key.age.pending` before any file is converted. Running `store encrypt` again finishes an interrupted run with the same passphrase.
- **Other commands:** `store passphrase` changes the passphrase (the key and files stay the same); `store decrypt` turns it off; `store info` and `doctor` report the state.
- **The audit mirror:** an encrypted audit log keeps no plaintext `audit.jsonl` mirror (it would hold what the encryption hides). Encrypting removes it, decrypting writes it again, and `audit verify` notes its absence. Export with `audit log --json`.
- **Backups:** they read the encrypted databases with the key. The audit copy stays encrypted, and the wrapped key file goes into the snapshot with it.
- **Platform:** needs the `sqlcipher` extra (`sqlcipher3-binary`), **Linux x86-64 only**.
- **Cost, measured on the build machine:** vault writes about 20% slower, reading a conversation about 7% slower. Audit appends got faster, because the mirror file isn't written.
- **Tested** with the real driver: content, schema and log targets absent from the files; wrong, missing and short keys refused; migrations, WAL and `secure_delete` on encrypted files; conversion both ways; an interrupted encryption finished; passphrase change; key by reference; the core's key read once and removed; backups of encrypted databases.
- **Tested in isolated mode with the real sandbox** (`tests/isolation/test_encrypted_storage.py`): the core opens the encrypted databases, and the key is in no core or wrapper process's `/proc/<pid>/environ`. **Not yet tried:** replication with encrypted databases.
- **Limits:** converting doesn't erase earlier plaintext from the disk's free space or old backups, so full-disk encryption is still advised. Tools that read the files directly stop working.

### 5.6 Supply chain
As v0.5 §5.7: hash-locked deps, pip-audit, license check, pinned CI actions, Scorecard, SBOM, Sigstore-signed releases, GGUF/safetensors only, pinned MCP servers.

**Implemented (T2.8):** `cairnward models pull <url> --sha256 <hex> [--name] [--env]` downloads one GGUF or safetensors file through cairnward-egress with a short-lived session from the running core (every host, including CDN redirects, is checked against that environment's network policy and can be approved in the inbox), into a private partial file; the file is kept only if its SHA-256 matches and its header is a real GGUF or safetensors header. Recorded as `model.download` in the audit log and in `models/manifest.json`; `cairnward models list` shows verified files. Ollama's own `ollama pull` still downloads directly from its registry, outside Cairnward; importing a verified GGUF into Ollama (`FROM <path>` in a Modelfile) avoids that.

---

## 6. Technology choices

| Area | Choice | License |
|---|---|---|
| Core | Python 3.12+, FastAPI/Uvicorn, pydantic | MIT / BSD |
| Egress proxy | Python first (`cairnward-egress`), Rust later | — |
| Sandbox | sandbox-runtime (`srt`, pinned; bubblewrap + seccomp + proxies; needs Node.js ≥ 22.12), Podman/Docker for server clients | Apache-2.0 / LGPL-2.0+ |
| Store | SQLite (WAL, FTS5, sqlite-vec), Alembic migrations | Public domain / MIT |
| Crypto | age via pyrage | MIT / Apache-2.0 |
| MCP | own minimal JSON-RPC layer for the tool proxy ([ADR-0015](adr/0015-mcp-proxy.md)); MCP Python SDK if resources/sampling are needed later | MIT |
| Redaction | built-in rules; Presidio (optional worker) | —; MIT |
| Backup / sync transport | restic, rclone, git | BSD-2 / MIT / GPL-2.0 (separate processes) |
| Dashboard | plain ES modules, no build step ([ADR-0016](adr/0016-dashboard-without-a-build-step.md)) | — |

---

## 7. Hardware and platforms
- Memory budget at 8K context: main 18.2 GB, fast 2.3 GB, embeddings ~0.6 GB, vision on demand (unloads the fast model), leaving ~8 GB for the OS, Cairnward, a client and light desktop use.
- Measured on the reference machine (D59; [results](bench/results-2026-11-01-ryzen3-3200g.md)): Ryzen 3 3200G, 4 cores, 29.3 GB usable RAM, no GPU, Debian 13.
  - `qwen3:30b`: reads ~20 tok/s, writes 11 → 2 tok/s as context grows to 4K; 18.2 GB at 8K context; loads in ~74 s.
  - `qwen3:1.7b`: reads ~50 tok/s, writes 17 → 6.5 tok/s; 2.3 GB; loads in ~2 s.
  - `qwen3:4b`: reads ~21 tok/s (same as the 30B MoE), writes 10 → 2.6 tok/s; 3.6 GB. Small dense models are only faster on CPU when they have fewer *active* parameters than the MoE model (~3B).
  - Load time of the 30B model: 33 s when cached by the OS, 74 s cold.
  - Follow-up turns with an unchanged prompt start: 18.6× faster (11.7 s vs 217 s at 4K).
  - Main + fast model fit together at 8K context only when heavy desktop apps are closed (~9 GB left).
- Consequences: 8K default context (D60); first turns of long prompts take minutes, follow-ups seconds (D62); heavy coding agents are slow (D64).
- **Upgrade paths** (documented for users, not required): an 8-core AM4 CPU (e.g. Ryzen 7 5700X, if the motherboard BIOS supports it) for roughly 2–3× faster prompt reading; a GPU with ≥ 12 GB VRAM for many times faster reading using llama.cpp's MoE offload (experts on CPU, rest on GPU); 64 GB RAM for larger contexts and keeping more models loaded (not faster).
- Performance targets are fixed from `scripts/bench_backend.py` results (§8).
- **Linux first** (Debian 12+, Ubuntu 22.04+, Mint, Pop!_OS). **Windows via WSL2** (D74, [guide](wsl2.md): Cairnward and sandboxed agents run inside WSL2; Windows-native clients connect to it over loopback). CI runs unit tests on Windows throughout so nothing becomes harder to port.

---

## 8. Non-functional requirements (summary)
Full list in [requirements.md](requirements.md).

| Area | Target |
|---|---|
| Gateway overhead | ≤ 75 ms, or ≤ 150 ms with isolation, added per model request under Strict (policy, audit, vault write), excluding model time |
| Model speed (measured, reference machine) | Baseline, not a target Cairnward controls: main model ~20 tok/s reading, 2–11 tok/s writing; fast model ~50 / 6.5–17 tok/s |
| Cache reuse | Follow-up turn in an unchanged conversation: ≥ 10× faster first token than a cold turn (measured 18.6×); the gateway must never break this |
| Responsiveness | Fast-model virtual models start answering a 512-token prompt in ≤ 15 s (measured 10 s); main model ≤ 30 s (measured 21 s) |
| Waiting-time warning | Requests expected to need > 60 s before the first token are flagged, using the stored benchmark profile |
| Footprint | Core ≤ 500 MB RAM without optional workers |
| Isolation | With `isolation = on`: direct connections from any sandbox fail (`cairnward doctor` and CI tests) |
| Reliability | No data loss on crash; audit record before every effect (when audit is on) |
| Privacy | Strict preset: zero outbound connections unless allowlisted; Balanced: none without approval; Open: as configured |
| Optionality | Every protection can be switched off independently and Cairnward still works; with all off, gateway overhead ≤ 10 ms per request |
| Accessibility | Dashboard WCAG 2.2 AA; single-key shortcuts can be turned off |
| Portability | Linux first; Windows via WSL2 (not yet run on a real machine); Python 3.12+ |

## 9. Delivery plan (summary)
Details in [plan/phase0.md](plan/phase0.md).
- **M0 — Gateway skeleton:** privacy settings model with presets and per-environment overrides (each protection added later plugs into it); model endpoint pass-through to Ollama with policy + audit + vault write; `cairnward run` sandbox with no direct network; `cairnward-egress`; one coding agent and one chat client working through it.
- **M1 — Research core (moved to the plugin since):** PDF library, `lit.ask` with page citations, citation check, CSL formatting, research job queue; PaperQA2 spike.
- **M2 — Security features and dashboard:** approvals inbox, risk tiers, provenance dialog, redaction, egress report, vault browser, locking, audit anchoring, MCP proxy with pinning, all four coding agents and 2–3 clients tested.
- **M3 — Sync, connectors, orchestration:** hardening, MCP SDK and CSL formatting, backup with purge rewrite, Zotero, academic APIs, full review pipeline, Docling/GROBID, hosted backends, Drive mirror ([plan/m3.md](plan/m3.md)). Multi-device replication (D72) and multi-agent workflows, deferred by D71, since built.
- **Later** (no date; earlier spec versions called this "Phase 3"): Ollama-compatible endpoint, a Rust rewrite of the egress proxy and audit log ([ADR-0003](adr/0003-python-now-rust-later.md); staged in [ADR-0020](adr/0020-rust-egress-and-audit.md), stage 1 under way in `rust/`). (The SQLCipher "encrypt everything" mode once listed here is built: D80.)

## 10. Testing strategy
As v0.5 §9, plus: **isolation tests** (every sandbox type tries direct DNS/TCP/UDP and must fail), **client compatibility tests** (scripted sessions for each tested client and coding agent against a fake model server), **vault reconstruction tests** (property-based: any sequence of stateless requests rebuilds the same conversation tree), **anchor tests** (tampering older than the last anchor is detected), **backup purge tests** (purged content absent from all restic snapshots afterwards), and **research evals** (zero invented references; quotations match PDFs).

## 11. Data and configuration contracts
As v0.5 §10 (pydantic-validated TOML with JSON Schema, Alembic migrations, OpenAPI for the dashboard/management API, versioned event-log format), plus the **client profile** format for `cairnward run` and `cairnward client add`.

## 12. Open questions
0a. Optional: does enabling Ollama flash attention (without KV quantisation) reduce the slowdown in generation speed with context on this CPU? If so, `cairnward doctor` recommends it.
0. Should the dashboard/endpoint hardening (loopback-only binding, Host/Origin checks) also be switchable? Currently not, because switching it off exposes Cairnward rather than reducing friction (§5.0).
1. ~~Third tested chat client~~: LibreChat (T2.9).
2. ~~Reuse `sandbox-runtime` or reimplement?~~ Decided in T0.6: reuse it ([ADR-0013](adr/0013-reuse-sandbox-runtime-for-isolation.md)).
3. ~~PaperQA2 as engine or own pipeline~~: own pipeline, PaperQA2 optional ([ADR-0014](adr/0014-literature-engine.md)).
4. Repo rename to `cairnward` (owner action, with the default-branch fix).
5. Drive folders for the first mirror (setup time).
6. ~~Own MCP layer vs MCP SDK; plain ES modules vs Svelte~~: decided 2026-11-02 (D65: move to the MCP SDK; D66: keep plain ES modules).

## 13. History
- v0.1–v0.5: all-in-one local agent with Claude-app features (see git history of this file).
- Critique (2026-10-31): scope too large for one person; most chat/coding features already exist (Open WebUI, Jan, opencode, Qwen Code, Aider, Goose, PaperQA2, Zori, ZotLLM); in-process egress control not enforceable; audit log not tamper-evident without external anchors; backups defeated crypto-shredding; heavy dependencies; name taken on PyPI. v0.6 responds to each point (D42–D54).

## 14. Risks
- **Weaker configurations look protected:** a user on Open or Custom may assume Strict-level guarantees. Mitigated by the badge, `privacy show`, *advisory only* warnings for ineffective combinations, and audit records of every change.
- **Client compatibility drift:** clients and agents change their APIs and config often. Mitigated by profiles, compatibility tests, and the generic OpenAI-compatible path.
- **Approval waits vs client timeouts:** long "ask" waits can time out clients. Mitigated by risk tiers, remembered approvals, and retry-aware approvals.
- **Content redaction blind spot:** third-party tools' own HTTPS traffic is only host-filtered. Mitigated by deny-by-default network, per-host approval, egress report; documented.
- **Vault duplicates client data:** two copies of conversations (vault + client). Mitigated by setup guides minimising client history and by the vault being lockable/purgeable.
- **CPU speed** (measured ~20 tok/s prompt reading on the main model): interactive coding with heavy agents is slow. Mitigated by cache-aware scheduling, 1.7B fast model, Aider-first recommendation, waiting-time warnings, optional hosted/LAN backends, and research designed as batch work.
- **Prompt injection:** limited impact by sandboxing, deny-by-default network, provenance dialogs, and audit; not fully preventable.
- **Sandbox bugs** (bubblewrap/namespace misconfiguration) would silently weaken the main guarantee. Mitigated by `cairnward doctor` isolation self-tests and CI isolation tests.
