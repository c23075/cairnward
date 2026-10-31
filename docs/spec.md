# Spec: Cairnward — a local privacy gateway and research agent

Status: **Draft v0.7, for review**
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

Most local AI tools (chat apps, coding agents, research assistants) run on your machine but give you **no verifiable record of what they did and no enforced control over what leaves the machine**. Cairnward sits between those tools and the outside world:

1. **Gateway:** one local endpoint for models (OpenAI-compatible) and tools (MCP). Every model call, tool call and outbound connection is checked against a policy, recorded in a tamper-evident audit log, and shown in an egress report.
2. **Enforced isolation:** tools run inside a sandbox whose **only** network route is Cairnward's egress proxy. This is enforced by the operating system, not by the tools' good behaviour.
3. **Transcript vault:** Cairnward keeps its own copy of every conversation that passes through it, which you can search, lock with a passphrase, sync and reliably delete, whichever client you used.
4. **Research workflows:** literature review (local PDFs, Zotero, academic APIs) as batch jobs that can run overnight on a CPU-only machine, with verified citations in APA/IEEE.

Primary use cases: **coding** (via existing coding agents run through the gateway) and **literature-review research**, equally.

All of these protections are **optional** (§5.0). With everything switched off, Cairnward is a plain model router with virtual models and research workflows; each protection can be switched on globally or per environment.

### 1.1 Goals
- G0. **User choice:** every privacy and security feature can be switched on or off, globally or per environment, through presets or individually; weakening is visible and logged, never silent.
- G1. Work fully offline with open-weight models (Qwen, DeepSeek) on a CPU-only 32 GB machine.
- G2. **Enforced choke point (when isolation is on):** no tool, client or Cairnward component can reach the network except through the egress proxy (OS-enforced).
- G3. Verifiable record: audit log with externally anchored checkpoints.
- G4. Client-neutral: any OpenAI-compatible chat client or coding agent works; a tested set is documented.
- G5. Research workflows that suit CPU hardware (batch, resumable, overnight) and never invent citations.
- G6. Pluggable model backends (local, LAN, hosted), with hosted denied by default.

### 1.2 Non-goals
- Building a chat app or a coding agent (use existing ones through the gateway).
- Multi-user/team server, remote access, cloud hosting.
- Training or fine-tuning.
- Native Windows in the first releases (Linux first; Windows via WSL2 later).

---

## 2. Decisions log

Current decisions. Superseded decisions are listed at the end with what replaced them. Major decisions have ADRs.

| # | Topic | Decision | ADR |
|---|---|---|---|
| D42 | Product direction | **Gateway + research**: client-neutral privacy gateway (model endpoint, MCP proxy, egress proxy, policy, audit, vault, dashboard) + own literature-review workflows | [0009](adr/0009-gateway-pivot.md) |
| D43 | Use cases | Coding and research equally; coding via existing agents (Qwen Code, opencode, Aider, Goose, any OpenAI-compatible agent) run through the gateway | 0009 |
| D44 | Clients | Client-neutral; 2–3 chat clients documented and tested; Cairnward builds only a **dashboard** (audit, egress, approvals, vault, research jobs, settings) | 0009 |
| D45 | Egress enforcement | **OS-level**: sandboxed processes have no network except a bridged connection to the egress proxy; Cairnward's own components run the same way; only the small `cairnward-egress` process has real network access | [0010](adr/0010-os-level-egress-enforcement.md) |
| D46 | User-only actions | Agent tools with a **confirmation dialog** in the dashboard/CLI that shows the action **and its provenance** (what content preceded the request) | — |
| D47 | Chat data | **Transcript vault**: Cairnward stores every conversation passing through it; lockable, syncable, purgeable | [0011](adr/0011-transcript-vault.md) |
| D48 | Effort and environment selection | **Virtual models** (`cw/<environment>:<effort>`) so any client's model picker selects effort + environment | — |
| D49 | Audit anchoring | Checkpoint hashes committed periodically to a **private git repo** the user owns | [0008](adr/0008-hash-chained-audit-log.md) (amended) |
| D50 | Backup purge | Purging a conversation also **rewrites restic snapshots** to remove it, then prunes | [0007](adr/0007-sync-restic-backup-and-event-log-replication.md) (amended) |
| D51 | Name, license | **Cairnward** (`cairnward`, alias `cw`), **Apache-2.0** | — |
| D52 | Platforms | **Linux (Debian family) first**; Windows via WSL2 in a later milestone; CI keeps unit tests on Windows | — |
| D53 | Heavy dependencies | Lean core; Docling, Presidio and other heavy libraries run as **optional workers**; pypdfium2 is the default PDF parser | — |
| D54 | Approvals | Risk-tiered, batched approvals with an approvals inbox, to avoid approval fatigue | — |
| D55 | Optional protections | **Every** privacy/security feature can be switched off, including isolation, network blocking, approvals, redaction, vault, audit log, anchoring, crypto-shredding, backup purge rewrite and MCP pinning | [0012](adr/0012-optional-privacy-controls-and-presets.md) |
| D56 | Defaults | **Preset chosen at first run** (`cairnward setup`): Open, Balanced or Strict; changeable any time; Custom when individual settings differ | 0012 |
| D57 | Granularity | **Global settings + per-environment overrides**; a virtual model (`cw/<env>:<effort>`) therefore also selects the privacy level | 0012 |
| D58 | Weakening | Allowed without friction, but **visible** (dashboard badge, `cairnward status`, `cairnward privacy show`) and **logged** (audit record of every change, written before audit itself is switched off) | 0012 |
| D5, D19 | Model backends | Pluggable: Ollama (default), llama.cpp, any OpenAI-compatible server; per role / environment / effort | [0004](adr/0004-pluggable-backends.md) |
| D29 | Hosted providers | Denied by default; tested first: Alibaba Model Studio, DeepSeek API, OpenRouter; Claude API later (separate API key) | 0004 |
| D7, D25 | Literature sources, citations | Local PDFs, Zotero, arXiv/Semantic Scholar/OpenAlex/PubMed/Crossref; CSL styles (APA, IEEE default) | — |
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

```
  Chat clients (Open WebUI, Jan, LibreChat, ...)      Coding agents (Qwen Code, opencode, Aider, Goose)
  in a container on an internal-only network          launched with `cairnward run -- <agent>`
            │                                                   │  (bubblewrap sandbox, no network
            │ OpenAI API / MCP                                  │   namespace interfaces except bridges)
 ┌──────────▼───────────────────────────────────────────────────▼────────────────────────────┐
 │ cairnward core (sandboxed: no direct network)                                             │
 │                                                                                            │
 │  Model endpoint (/v1, virtual models)   MCP proxy (aggregated, pinned tools)              │
 │            │                                   │                                           │
 │            └──────────────┬────────────────────┘                                           │
 │                   ┌───────▼────────┐    approvals inbox ──► dashboard / CLI / notification │
 │                   │ POLICY ENGINE  │                                                       │
 │                   └───────┬────────┘                                                       │
 │     ┌──────────┬──────────┼───────────┬─────────────┬──────────────┐                       │
 │  AUDIT LOG  TRANSCRIPT  REDACTOR   RESEARCH     ARTIFACTS     SYNC/BACKUP                  │
 │  (hash chain) VAULT      (light +  WORKFLOWS    (git repos)   (restic, event logs)         │
 │               (age)     optional   (jobs queue)                                            │
 │                          worker)                                                           │
 │  Dashboard (loopback, hardened)                                                            │
 └──────────────┬──────────────────────────────────────────────────────────────┬──────────────┘
                │ Unix socket                                                  │ Unix socket
        ┌───────▼─────────┐                                           ┌────────▼─────────┐
        │ Model server    │                                           │ cairnward-egress │──► Internet
        │ Ollama/llama.cpp│                                           │ (only process    │   (allowlisted
        │ (local)         │                                           │  with network)   │    hosts only)
        └─────────────────┘                                           └──────────────────┘
```

The diagram shows the **Strict** configuration. Each box below the policy engine, and the sandboxing itself, can be switched off (§5.0); the endpoints, routing, virtual models and research workflows always work.

**Rules** (when the corresponding protections are on)
1. Clients and agents talk only to Cairnward (model endpoint, MCP proxy) and, if allowed, to the egress proxy. The OS enforces this when isolation is on (§3.2); with isolation off, the proxy settings are advisory and tools can connect directly.
2. Every effect (model call, tool call, outbound connection, file write by Cairnward) goes through `policy.check → allow | ask | deny`, with an audit record before and after (when audit is on). Settings that are off make the policy engine return `allow` for that category without asking.
3. `cairnward-egress` is the only process with real network access. It is small, does host allowlisting and connection logging, and is the first component to be rewritten in Rust (D9).
4. The local model server is reached through a Unix socket or loopback port that is bridged into the sandbox; it never needs internet access after models are downloaded.

### 3.1 Local API and dashboard security
Unchanged from v0.5 ([ADR-0005](adr/0005-local-core-service-and-api-hardening.md)): Unix socket for the CLI, loopback-only HTTP for the dashboard with Host allowlist, Origin checks, CSRF tokens, `SameSite=Strict` cookies, strict CSP, no CORS, single-use login links. The model endpoint and MCP proxy accept per-client **API keys** issued by Cairnward (`cairnward client add open-webui`), so the audit log knows which client made each request and each client can have its own policy.

### 3.2 OS-level egress enforcement (D45, [ADR-0010](adr/0010-os-level-egress-enforcement.md))
Linux mechanism (same approach as Anthropic's open-source `sandbox-runtime`; reusing it directly is evaluated in task T0.6):
- **Agents and CLI tools** (`cairnward run -- qwen`) run under **bubblewrap** with a **new network namespace that has only a loopback interface**. Inside it, Cairnward's model endpoint, MCP proxy and HTTP(S) proxy are reachable through Unix sockets bridged to loopback ports. `HTTP_PROXY`/`HTTPS_PROXY` point at the bridge. Anything that ignores the proxy settings simply has no route out.
- Filesystem: the workspace is mounted read-write; the agent's own config directory read-write; everything else read-only or hidden (`~/.ssh`, `~/.aws`, browser profiles, other projects are hidden by default).
- **Server-style clients** (Open WebUI, LibreChat) run in **Podman/Docker containers on an internal network** whose only member besides the client is Cairnward. Templates (`compose` files) are provided.
- **Cairnward itself** runs its core in the same kind of namespace, so its own libraries (e.g. Docling or Presidio fetching models) can't bypass the proxy. Model and data downloads go through `cairnward-egress` like everything else and are audited.
- **What the proxy can see:** host names (from CONNECT / SNI), bytes and timing for all connections. **Content** is visible only for traffic Cairnward originates or terminates itself (model calls, MCP tool calls, research API calls), so **content redaction applies to those**. For third-party tools' own HTTPS connections, policy is host-level allow/ask/deny only. TLS interception with a local CA is possible but off by default and not recommended (it weakens security for the tool).
- `cairnward doctor` verifies isolation by attempting direct connections from inside each sandbox and expecting failure.

---

## 4. Components

### 4.1 Model endpoint and backends
- OpenAI-compatible endpoint: `/v1/chat/completions` (streaming, tools), `/v1/embeddings`, `/v1/models`. An Ollama-compatible endpoint (`/api/chat`, `/api/tags`) is a later option for clients that only speak Ollama.
- Backends as in v0.5: named entries in `backends.toml` (`ollama`, `openai_compat` drivers; `location = local | lan | hosted`); hosted denied by default, redacted, ☁-marked, never used for locked conversations; fallback only to local backends.

**Virtual models (D48).** `/v1/models` lists one entry per environment × effort level, e.g.:

| Virtual model | Resolves to |
|---|---|
| `cw/coding:low` | fast model (Qwen3-4B class), no thinking, low limits |
| `cw/coding:high` | main model (Qwen3-30B-A3B class), thinking on, larger budgets |
| `cw/litreview-offline:medium` | main model, research tools allowed, network off |
| `cw/raw/qwen3:30b` | pass-through to a specific model with the environment `default` |

Picking a virtual model in any client selects effort (thinking mode and budget, sampling, context size, per-request token/time limits) and environment (allowed tools, network policy, redaction, backend). Effort levels and environments remain user-definable TOML (`effort.toml`, `environments/*.toml`; schemas as in v0.5).
- Model defaults are re-checked against current releases at each milestone (Qwen 3.5 models are now available in Ollama). Default roles: main ≈ Qwen3-30B-A3B or its successor, fast ≈ 4B class, vision ≈ small Qwen-VL on demand, embeddings ≈ Qwen3-Embedding-0.6B.
- **Context reporting:** every response carries standard `usage` counts (shown by most clients), and the dashboard's live view shows context used vs limit per conversation, with a warning before a request that is expected to need more than 60 s of prompt processing (from benchmark data).

### 4.2 MCP proxy and tools
- Clients connect to one MCP endpoint (streamable HTTP, or `cairnward mcp` as a stdio shim). Cairnward aggregates the MCP servers enabled for the client's environment and applies policy, audit and redaction to every call.
- Third-party MCP servers run in their own sandboxes (§3.2) with only their declared permissions.
- Pinning (v0.5 D38): pinned versions and hashes; tool name/description/schema hashed at approval; any change disables the tool until re-approved; descriptions scanned for instruction-like text.
- Built-in Cairnward tools (exposed via the proxy): `lit.*` (research, §4.6), `vault.search`, `artifact.write`, `cite.format`, `citation.check`, `encrypt` (user-only, §5.3).
- Skills: SKILL.md folders are served to clients that support them and used by Cairnward's own research workflows.

### 4.3 Coding agents through the gateway (D43)
- `cairnward run [--env coding] -- <agent> [args]` starts any agent in the sandbox with its model base URL, API key and MCP config pointed at Cairnward. Ready-made profiles for **Qwen Code, opencode, Aider and Goose** (all four tested); any OpenAI-compatible agent works with a generic profile.
- Approvals for edits and commands stay inside the agent (each has its own). Cairnward adds:
  - sandboxing (filesystem and network),
  - an audit of every model and tool call, linked to the run,
  - **workspace snapshots:** before and after each run, Cairnward commits the workspace state to a hidden ref (`refs/cairnward/runs/<id>`), so every change an agent made is reviewable and revertible (`cairnward runs diff <id>`, `cairnward runs revert <id>`),
  - per-run budgets (tokens, time, requests) enforced at the model endpoint.

### 4.4 Chat clients (D44)
- Documented and tested: **Open WebUI** (check its current license's branding terms), **Jan**, and one more chosen at M2 (LibreChat or AnythingLLM). Each gets a setup guide: base URL, per-client API key, MCP endpoint, recommended privacy settings (telemetry off, history minimal or temporary chats, since the vault holds the authoritative copy).
- Features like chat folders, uploads and artifacts panels come from the client. Effort/environment come from virtual models (§4.1).

### 4.5 Transcript vault (D47, [ADR-0011](adr/0011-transcript-vault.md))
- Every request/response through the model endpoint is stored. Conversations are reconstructed from the OpenAI API's stateless requests by **prefix hashing**: each request carries the full history, so a request whose message prefix matches a stored conversation extends it; a different prefix starts a new conversation or a branch.
- Records are encrypted per conversation with age keys (crypto-shredding; v0.5 §5.5). Conversations can be **locked** with a passphrase (D23/D24), the same as v0.5's chats.
- **Vault browser** in the dashboard: search (FTS), folders, pin, lock, export, delete, with the keyboard shortcuts from v0.5 (`j/k`, `/`, `d` delete with confirm and undo, `f` move to folder, `L` lock, `x` egress report, `l` audit, `?` help; single-key shortcuts can be turned off).
- Deleting in the vault does **not** delete the copy in the client's own history; the dashboard says so, and setup guides show how to minimise client-side history.
- Vault retention per environment (`keep forever` default; or N days); `vault = "off"` per environment stores only audit digests.

### 4.6 Research workflows (D7, D25, G5)
- **Jobs**, not chat turns: `cairnward research review --question "..." --library thesis --style apa` queues a job; the dashboard shows progress; jobs are **resumable** after a restart and can be scheduled ("run overnight"). On CPU, a review of tens of papers is expected to take hours; that's the design point.
- Pipeline: library (local PDF folder, Zotero via local API) and optional search (arXiv, Semantic Scholar, OpenAlex, PubMed, Crossref, network opt-in) → dedupe → screen (criteria) → extract per paper (claims, methods, datasets, limitations) → synthesise → **citation check** (every reference must resolve to an indexed paper or DOI; every quotation must match the PDF text) → format (CSL: APA, IEEE; `.csl` files add styles).
- Outputs: comparison table, synthesis draft, `.bib`/CSL-JSON, written to a git-versioned artifacts folder.
- Interactive use: the same capabilities are exposed as MCP tools (`lit.search`, `lit.ask` with page-cited answers, `lit.review.start`), so any chat client can use them.
- **Engine:** PaperQA2 (Apache-2.0) is evaluated as the question-answering engine in a spike (task T1.1), configured to call Cairnward's model endpoint; if it doesn't fit (dependencies, CPU speed, control), Cairnward's own pipeline is used. Other prior art studied: Zori, ZotLLM (citation and quote checking), STORM, GPT Researcher.
- **Multi-agent orchestration** lives here: workflow steps can fan out per paper as sub-agents (queued, `max_parallel` per backend, 1 on local CPU) with budgets and nested audit records. Workflows are YAML.

### 4.7 Artifacts
Git repo per conversation, research job and agent run (one commit per write; restore = new commit). Previews of HTML/SVG in the dashboard use a sandboxed origin with no network.

### 4.8 Sync and backup (D27, D30, D40, D50)
As v0.5 ([ADR-0007](adr/0007-sync-restic-backup-and-event-log-replication.md)), now covering the **vault, research library and artifacts**:
- Modes `none` (default), `on_demand`, `auto` (on change and/or schedule) for every job.
- `backup` jobs: restic (always encrypted), retention policies.
- **Purge rewrites backups (D50):** purging a conversation runs `restic rewrite --exclude` for its records and key across all snapshots, then `restic prune`. Until that completes the dashboard shows "pending removal from N backups"; failures are retried and reported.
- `replicate` jobs: per-device append-only event logs + git; locked items only ever written encrypted; cloud warning + "encrypt this sync target" option.
- Drive mirror (incoming) via rclone, read-only, with preview.

### 4.9 Dashboard
Loopback web app (hardened as §3.1), the only UI Cairnward builds: **approvals inbox**, live activity, audit browser (per client/run/conversation), egress report, vault browser, research jobs, artifacts, sync status, settings (backends, environments, effort, clients, MCP servers). Plus a terminal equivalent for key flows: `cairnward approvals`, `cairnward audit`, `cairnward vault`. Accessibility target WCAG 2.2 AA.

---

## 5. Security and privacy

### 5.0 Privacy controls and presets (D55–D58, [ADR-0012](adr/0012-optional-privacy-controls-and-presets.md))
Every protection in this section and in §3.2/§4.5/§4.8 is a setting. A **preset** sets them all at once; individual settings can then be changed (the preset shows as *Custom*).

**Settings and presets**

| Setting (`[privacy]` key) | Values | Open | Balanced | Strict |
|---|---|---|---|---|
| `isolation`: OS sandbox + network namespaces for agents, clients and the core (§3.2) | `on` / `off` | off | on | on |
| `network`: outbound access for tools and Cairnward (§3.2) | `open` / `ask` (new hosts need approval) / `allowlist` / `off` | open | ask | allowlist (empty by default = off) |
| `hosted_backends` (§4.1) | `allow` / `ask` / `deny` | allow | ask | deny |
| `approvals`: risk-tiered prompts (§5.3) | `off` / `high_only` / `tiered` | off | high_only | tiered |
| `redaction` (§5.4) | `off` / `warn` (log only) / `enforce` (secrets block, PII ask) | off | warn | enforce |
| `vault`: transcript vault (§4.5) | `off` / `digests` / `full` | off | full | full |
| `audit`: hash-chained audit log (§5.2) | `off` / `on` | on | on | on |
| `audit_anchor`: external checkpoints (§5.2) | `off` / `git` | off | off | git |
| `crypto_shred`: per-conversation keys for reliable deletion (§5.5) | `off` / `on` | off | on | on |
| `backup_purge`: purge rewrites backups (§4.8) | `off` / `on` / `ask` | off | ask | on |
| `mcp_pinning`: tool pinning and rug-pull checks (§4.2) | `off` / `on` | off | on | on |
| `egress_report`: per-conversation report (§4.9) | `off` / `on` | off | on | on |

- **Open** keeps the audit log on because it costs nothing in daily use; it can still be switched off. With every setting off, Cairnward is a plain router: virtual models, backends, research workflows and the dashboard's model/job views keep working.
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

# environments/thesis-confidential.toml
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
- Optional OpenTelemetry GenAI export (local); Merkle checkpoints in Phase 3.

### 5.3 Policy and approvals (D46, D54)
- `allow | ask | deny` per action type, environment and client.
- **Approvals inbox:** "ask" decisions appear in the dashboard, as a desktop notification, and in `cairnward approvals`. The request waits (clients may time out on long waits; the inbox shows "client gave up" and the approval applies to the retry).
- **Avoiding approval fatigue (D54):** actions are risk-tiered: *low* (reads in workspace, GET to an already-approved academic API) → allowed and logged; *medium* (new host, write outside artifacts) → ask once per host/scope with "remember for this environment"; *high* (user-only actions, hosted backend, sending detected secrets) → always ask, never remembered. Similar pending approvals are batched into one prompt.
- **User-only actions (D46):** `encrypt`/`decrypt`, sync job changes, enabling hosted backends, adding MCP servers, unsandboxed runs. They are agent tools, but each call opens a **confirmation dialog in Cairnward's own UI** (never in the client), showing the action, the conversation, and its **provenance**: the content that entered the context since your last message (e.g. "after reading `paper.pdf` page 4" or "after web page example.com"). If anything untrusted came after your last message, the dialog says so prominently. Passphrases are typed only in this dialog.

### 5.4 Redaction
- Applies to traffic Cairnward terminates: model calls to non-local backends, MCP tool calls, research API calls.
- Built-in light scanner (secret patterns, emails, phone numbers, ID-like numbers, custom regexes) in the core; **Presidio** as an optional worker for richer personal-data detection (D53).
- Defaults: secrets **block**, personal data **ask** (batched; see §5.3).

### 5.5 Encryption and deletion
As v0.5 ([ADR-0006](adr/0006-encryption-age-key-wrapping-crypto-shredding.md)): age, per-conversation keys, opt-in passphrase locks (conversations and artifacts separately), crypto-shredding on purge, plus backup rewrite (D50). Stated limits: unlocked conversations' search index is plaintext in SQLite; client-side copies are outside Cairnward's control.

### 5.6 Supply chain
As v0.5 §5.7: hash-locked deps, pip-audit, license check, pinned CI actions, Scorecard, SBOM, Sigstore-signed releases, GGUF/safetensors only, pinned MCP servers.

---

## 6. Technology choices

| Area | Choice | License |
|---|---|---|
| Core | Python 3.12+, FastAPI/Uvicorn, pydantic | MIT / BSD |
| Egress proxy | Python first (`cairnward-egress`), Rust later | — |
| Sandbox | bubblewrap (+ Unix-socket bridges; evaluate `sandbox-runtime`), Podman/Docker for server clients | LGPL-2.0+ / Apache-2.0 |
| Store | SQLite (WAL, FTS5, sqlite-vec), Alembic migrations | Public domain / MIT |
| Crypto | age via pyrage | MIT / Apache-2.0 |
| MCP | MCP Python SDK | MIT |
| PDF | pypdfium2 (default); Docling (optional worker) | Apache-2.0/BSD; MIT |
| Redaction | built-in rules; Presidio (optional worker) | —; MIT |
| Citations | citeproc-py + CSL styles | BSD / CC BY-SA |
| Research engine | own pipeline; PaperQA2 evaluated | Apache-2.0 |
| Backup / sync transport | restic, rclone, git | BSD-2 / MIT / GPL-2.0 (separate processes) |
| Dashboard | Svelte, built offline and served by the core | MIT |
| Agent loop for research workflows | evaluate PydanticAI (MCP + OpenTelemetry built in) vs own loop | MIT |

---

## 7. Hardware and platforms
- Reference machine: **32 GB RAM, CPU only, Debian**. Memory budget as in v0.5 §4.0 (main ≈ 19 GB, fast ≈ 2.6 GB, embeddings ≈ 0.6 GB, vision on demand, ~5 GB for OS/Cairnward/client).
- CPU realities shape the design: interactive coding with a 30B-class model on CPU is slow per step, so per-run budgets and the context warning matter; research runs as batch jobs.
- Performance targets are fixed from `scripts/bench_backend.py` results (§8).
- **Linux first** (Debian 12+, Ubuntu 22.04+, Mint, Pop!_OS). **Windows later via WSL2** (Cairnward and sandboxed agents run inside WSL2; Windows-native clients connect to it over loopback). CI runs unit tests on Windows throughout so nothing becomes harder to port.

---

## 8. Non-functional requirements (summary)
Full list in [requirements.md](requirements.md).

| Area | Target |
|---|---|
| Gateway overhead | ≤ 50 ms added per model request (policy, audit, vault write), excluding model time |
| Model speed | Provisional until benchmarked: main model first token ≤ 10 s for a 2K-token prompt (warm); warning before requests needing > 60 s of prompt processing |
| Footprint | Core ≤ 500 MB RAM without optional workers |
| Isolation | With `isolation = on`: direct connections from any sandbox fail (`cairnward doctor` and CI tests) |
| Reliability | No data loss on crash; audit record before every effect (when audit is on); research jobs resume after restart |
| Privacy | Strict preset: zero outbound connections unless allowlisted; Balanced: none without approval; Open: as configured |
| Optionality | Every protection can be switched off independently and Cairnward still works; with all off, gateway overhead ≤ 10 ms per request |
| Accessibility | Dashboard WCAG 2.2 AA; single-key shortcuts can be turned off |
| Portability | Linux first; Windows via WSL2 later; Python 3.12+ |

## 9. Delivery plan (summary)
Details in [plan/phase0.md](plan/phase0.md).
- **M0 — Gateway skeleton:** privacy settings model with presets and per-environment overrides (each protection added later plugs into it); model endpoint pass-through to Ollama with policy + audit + vault write; `cairnward run` sandbox with no direct network; `cairnward-egress`; one coding agent and one chat client working through it.
- **M1 — Research core:** PDF library, `lit.ask` with page citations, citation check, CSL formatting, research job queue; PaperQA2 spike.
- **M2 — Security features and dashboard:** approvals inbox, risk tiers, provenance dialog, redaction, egress report, vault browser, locking, audit anchoring, MCP proxy with pinning, all four coding agents and 2–3 clients tested.
- **M3 — Sync, connectors, orchestration:** backup with purge rewrite, replication, Zotero, academic APIs, Drive mirror, research workflows with sub-agents, hosted backends.
- **Later:** Windows via WSL2, Ollama-compatible endpoint, Rust egress proxy, Merkle checkpoints.

## 10. Testing strategy
As v0.5 §9, plus: **isolation tests** (every sandbox type tries direct DNS/TCP/UDP and must fail), **client compatibility tests** (scripted sessions for each tested client and coding agent against a fake model server), **vault reconstruction tests** (property-based: any sequence of stateless requests rebuilds the same conversation tree), **anchor tests** (tampering older than the last anchor is detected), **backup purge tests** (purged content absent from all restic snapshots afterwards), and **research evals** (zero invented references; quotations match PDFs).

## 11. Data and configuration contracts
As v0.5 §10 (pydantic-validated TOML with JSON Schema, Alembic migrations, OpenAPI for the dashboard/management API, versioned event-log format), plus the **client profile** format for `cairnward run` and `cairnward client add`.

## 12. Open questions
0. Should the dashboard/endpoint hardening (loopback-only binding, Host/Origin checks) also be switchable? Currently not, because switching it off exposes Cairnward rather than reducing friction (§5.0).
1. Third tested chat client: LibreChat or AnythingLLM (decide at M2).
2. Reuse Anthropic's `sandbox-runtime` (Node.js) directly, or reimplement its approach in Python (decide in T0.6, ADR-0013).
3. PaperQA2 as engine or own pipeline (decide in T1.1).
4. Repo rename to `cairnward` (owner action, with the default-branch fix).
5. Drive folders for the first mirror (setup time).

## 13. History
- v0.1–v0.5: all-in-one local agent with Claude-app features (see git history of this file).
- Critique (2026-10-31): scope too large for one person; most chat/coding features already exist (Open WebUI, Jan, opencode, Qwen Code, Aider, Goose, PaperQA2, Zori, ZotLLM); in-process egress control not enforceable; audit log not tamper-evident without external anchors; backups defeated crypto-shredding; heavy dependencies; name taken on PyPI. v0.6 responds to each point (D42–D54).

## 14. Risks
- **Weaker configurations look protected:** a user on Open or Custom may assume Strict-level guarantees. Mitigated by the badge, `privacy show`, *advisory only* warnings for ineffective combinations, and audit records of every change.
- **Client compatibility drift:** clients and agents change their APIs and config often. Mitigated by profiles, compatibility tests, and the generic OpenAI-compatible path.
- **Approval waits vs client timeouts:** long "ask" waits can time out clients. Mitigated by risk tiers, remembered approvals, and retry-aware approvals.
- **Content redaction blind spot:** third-party tools' own HTTPS traffic is only host-filtered. Mitigated by deny-by-default network, per-host approval, egress report; documented.
- **Vault duplicates client data:** two copies of conversations (vault + client). Mitigated by setup guides minimising client history and by the vault being lockable/purgeable.
- **CPU speed** for interactive coding. Mitigated by fast-model virtual models, budgets, context warnings; research designed as batch work.
- **Prompt injection:** limited impact by sandboxing, deny-by-default network, provenance dialogs, and audit; not fully preventable.
- **Sandbox bugs** (bubblewrap/namespace misconfiguration) would silently weaken the main guarantee. Mitigated by `cairnward doctor` isolation self-tests and CI isolation tests.
