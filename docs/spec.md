# Spec: Cairn — a local, privacy-first AI agent

Status: **Draft v0.5, for review** (v0.5: aligned with industry practice: threat model, local API hardening, age + key wrapping, crypto-shredding, MCP pinning, accessibility, ADRs, numbered requirements, NFRs, testing strategy, API/config contracts, sync rework, supply chain; D32–D41)
Date: 2026-10-31
Repo: `c23075/possessed-incantation`
Name: **Cairn** (stacked stones that mark a trail, i.e. the audit trail). CLI command: `cairn`.

**Companion documents**
- [`requirements.md`](requirements.md): numbered functional (FR-) and non-functional (NFR-) requirements with MoSCoW priority and verification.
- [`threat-model.md`](threat-model.md): STRIDE + LINDDUN threat model, mapped to OWASP Top 10 for LLM Applications (2025).
- [`adr/`](adr/): Architecture Decision Records for the major decisions.
- [`plan/phase0.md`](plan/phase0.md): Phase 0 milestones (M0–M3) and ordered tasks.
- [`../scripts/bench_backend.py`](../scripts/bench_backend.py): local speed benchmark that sets the performance NFRs.

---

## 1. Purpose

A local AI agent for **coding** and **literature-review research**, with a feature
set similar to the Claude app, that runs on open-weight models on the user's own
hardware. **Privacy and security come first**: every action the agent takes is
recorded in a log that shows any later tampering, nothing leaves the machine
unless explicitly allowed, and data on disk is encrypted when the user chooses (with reliable deletion either way).

### 1.1 Goals
- G1. Run fully offline by default on open-weight models with performance similar to `qwen3:30b` (Qwen3-30B-A3B) on a CPU or mixed CPU/GPU machine.
- G2. **Single choke point:** every model call, tool call, file access and outbound request goes through one core that checks it against policy and writes it to the audit log.
- G3. Claude-app-like experience: effort selector, environment picker, artifacts, context meter, uploads, connectors, web search, tools, skills, chat folders, single-key shortcuts.
- G4. Two clients from day one: a **full-screen terminal app (TUI)** and a **local web UI**, both thin clients of the same core API.
- G5. Pluggable model servers, open-source infrastructure only.
- G6. Multi-agent: the main agent delegates to sub-agents **and** user-defined workflows can be run.

### 1.2 Non-goals (v1)
- Multi-user / team server, remote access, cloud hosting.
- Training or fine-tuning models.
- Proprietary model APIs (may be added later as an opt-in "remote backend", and audited the same way).
- Mobile clients.

---

## 2. Decisions log (from interview)

| # | Topic | Decision |
|---|---|---|
| D1 | Build approach | **Hybrid**: our own core + policy/audit layer; reuse open-source libraries for model serving, tools (MCP), parsing and retrieval |
| D2 | Interface | **TUI and web UI from day one**, both talking to a local core service |
| D3 | Hardware | CPU only, 32 GB RAM (see D21, §4.0) |
| D4 | Network default | **Off by default, opt-in** per environment/chat; every outbound call logged |
| D5 | Model server | **Pluggable**. Default: Ollama. Performance driver: llama.cpp `llama-server`. Any OpenAI-compatible endpoint works |
| D6 | Coding autonomy | **Edit + sandboxed run**: diffs need approval; commands run in a container with no network unless allowed |
| D7 | Literature sources | Local PDFs, Zotero, arXiv / Semantic Scholar / OpenAlex, PubMed / Crossref |
| D8 | Must-have security | Tamper-evident action log, outbound redaction, encryption at rest, per-chat egress report |
| D9 | Stack | **Python now, Rust later** (audit log + egress proxy are candidates to move to Rust) |
| D10 | Multi-agent | **Both**: main-agent delegation and YAML workflows |
| D11 | Context default | **32K, adjustable**, with meter + auto-compaction + retrieval |
| D12 | Effort | Controls thinking mode, thinking budget, tool/sub-agent budget **and model choice** |
| D13 | Environment | **Profile bundle** (workspace, tools, connectors, network policy, sandbox, models) |
| D14 | Connectors | **Sync to local first**: Drive folders mirrored, repos cloned; the agent reads only local copies |
| D15 | Skills/tools | **SKILL.md folders + MCP servers**, each declaring the permissions it needs |
| D16 | Artifacts | **Versioned folder + preview** |
| D17 | MVP | **Thin slice of everything** |
| D18 | Spec location | This repo, `docs/spec.md` on `main` |
| D19 | Backend swapping | Backends can be swapped **per environment, per model role and per sub-agent**, including **hosted** OpenAI-compatible servers (opt-in, audited, redacted) |
| D20 | Effort levels | Default 3 levels plus **any number of user-defined levels** in TOML |
| D21 | Hardware target | **32 GB RAM, CPU only, no GPU.** Debian first; must also run on Windows (10/11) and Debian-based distros (Ubuntu, Mint, Pop!_OS, ...) |
| D22 | Artifact versioning | **A git repo per chat** under the artifacts root |
| D23 | Encryption | **Off by default.** Only the user turns it on: directly in the UI/CLI, or by explicitly telling the agent to run the `encrypt` tool. The agent never encrypts by itself and never sees passphrases |
| D24 | Passphrases | **Only for chats the user locks**; everything else opens without a passphrase |
| D25 | Citation styles | APA + IEEE by default via **CSL style files**; more styles added by dropping in `.csl` files; a `references` skill uses the formatter tool |
| D26 | Name | **Cairn**, CLI `cairn` |
| D27 | Sync modes | Every sync job (incoming Drive/git mirrors **and** outgoing chat/artifact sync) has a mode: **none** (default), **on demand**, or **automatic/scheduled** (on change and/or on a schedule) |
| D28 | Windows sandbox | Default **WSL2 + bubblewrap**; Podman/Docker or Windows Sandbox used when available; **restricted-host mode off unless enabled**, and then every command needs approval |
| D30 | Chat + artifact sync | **Chats and artifacts can both be synced** (backup / multi-device) to a chosen target: local folder/NAS, Google Drive, or a private git remote |
| D31 | Encryption scope | **Opt-in** encryption for chats **and** artifacts, chosen separately. Synced copies of encrypted items stay encrypted (encrypted on this machine before upload) |
| D29 | Hosted providers (tested first) | Alibaba Model Studio, DeepSeek API, OpenRouter; **Claude API later** via a separate Anthropic API key (a Claude Pro subscription doesn't include API access, and logging in with a consumer account in third-party apps isn't supported) |
| D32 | Decision records | Major decisions get an ADR in `docs/adr/`; this table stays as the index ([ADR-0001](adr/0001-record-architecture-decisions.md)) |
| D33 | Threat modelling | STRIDE (security) + LINDDUN (privacy), mapped to OWASP Top 10 for LLM Applications 2025 ([threat-model.md](threat-model.md)) |
| D34 | Local API hardening | Unix socket by default; loopback HTTP for the web UI with Host allowlist, Origin checks, CSRF token, strict CSP ([ADR-0005](adr/0005-local-core-service-and-api-hardening.md)) |
| D35 | Encryption format | **age** (via pyrage) with a key hierarchy: per-chat key wrapped by passphrase (scrypt) or recovery key ([ADR-0006](adr/0006-encryption-age-key-wrapping-crypto-shredding.md)) |
| D36 | Deletion | **Crypto-shredding**: every chat store record is encrypted with its own key even when not locked, kept without a passphrase, and deleted on purge. Passphrases stay opt-in (D23/D24) |
| D37 | Tracing | Audit log stays hash-chained; optional **OpenTelemetry GenAI** export to a local file/collector; Merkle checkpoints in Phase 3 |
| D38 | MCP trust | Pin MCP server versions + hashes; record a hash of each tool's name/description/schema at approval; any change disables the tool until re-approved |
| D39 | Accessibility | Single-key shortcuts can be turned off (WCAG 2.1.4); web UI targets **WCAG 2.2 AA** |
| D40 | Sync design | **restic** for encrypted backups; **per-device append-only event logs** for multi-device chat sync; git for artifacts ([ADR-0007](adr/0007-sync-restic-backup-and-event-log-replication.md)) |
| D41 | Contracts and supply chain | OpenAPI contract for the core API, pydantic-validated config with JSON Schema, versioned DB migrations; SBOM (CycloneDX), signed releases (Sigstore), OpenSSF Scorecard, pip-audit, GGUF/safetensors-only models |

---

## 3. Architecture

```
 ┌──────────────┐   ┌──────────────┐
 │  TUI client  │   │ Web UI client│        (thin clients: no direct
 │  (Textual)   │   │ (Svelte SPA) │         model, file or network access)
 └──────┬───────┘   └──────┬───────┘
        │  HTTP + WebSocket on a Unix socket / 127.0.0.1, auth token
 ┌──────▼──────────────────▼─────────────────────────────────────────┐
 │                         cairn core (daemon)                       │
 │  ┌───────────┐ ┌────────────┐ ┌────────────┐ ┌──────────────────┐  │
 │  │ Chat/Agent│ │Orchestrator│ │ Context mgr│ │ Skills + MCP host│  │
 │  │   loop    │ │(sub-agents,│ │(meter,     │ │ (tool registry)  │  │
 │  │           │ │ workflows) │ │ compaction)│ │                  │  │
 │  └─────┬─────┘ └─────┬──────┘ └─────┬──────┘ └────────┬─────────┘  │
 │        └─────────────┴──────┬───────┴─────────────────┘            │
 │                    ┌────────▼────────┐                             │
 │                    │  POLICY ENGINE  │◄── environment profile       │
 │                    │  (allow/ask/deny)│                             │
 │                    └────────┬────────┘                             │
 │        ┌──────────────┬─────┴────────┬──────────────┐              │
 │  ┌─────▼─────┐ ┌──────▼──────┐ ┌─────▼──────┐ ┌─────▼──────┐       │
 │  │ AUDIT LOG │ │ Egress proxy│ │ FS gateway │ │  Sandbox   │       │
 │  │(hash chain)│ │ + redactor │ │(workspace  │ │ runner     │       │
 │  └───────────┘ └──────┬──────┘ │ allowlist) │ │(Podman)    │       │
 │                       │        └────────────┘ └────────────┘       │
 │  Store: SQLite (chats, memory, audit, vector index); locked chats encrypted   │
 └───────────────────────┼────────────────────────────────────────────┘
        ┌────────────────┼──────────────┐
  ┌─────▼──────┐   ┌─────▼──────┐  ┌────▼──────────┐
  │Model server│   │ Internet   │  │ Sync workers  │
  │ (Ollama /  │   │ (SearXNG,  │  │ (Drive mirror,│
  │ llama.cpp) │   │ arXiv, ...)│  │  git fetch)   │
  └────────────┘   └────────────┘  └───────────────┘
```

**Rules**
1. Clients never touch the model, files or network directly; they only call the core API.
2. Every effect goes through `policy.check(action) → allow | ask | deny` and then `audit.append(...)` **before** it runs, with the result appended **after**.
3. All outbound HTTP goes through the egress proxy (redaction + host allowlist). MCP servers and sandboxes run with no network, or with network only through that proxy.
4. The model server is treated as untrusted-but-local: it's only reachable at an address that never leaves the machine (`127.0.0.1` or a Unix socket), and the core warns if it is configured to listen on other network interfaces.

### 3.1 Local API security (D34)
Local AI servers have been attacked from ordinary web pages (DNS rebinding, cross-site requests to `localhost`). The core API therefore:
- **Listens on a Unix domain socket by default** (file mode `0600`; AF_UNIX also works on Windows 10 1803+). The TUI and CLI use it.
- The web UI uses a **loopback HTTP port** (`127.0.0.1` / `::1` only; never `0.0.0.0`) with:
  - **Host header allowlist** (`localhost`, `127.0.0.1`, `[::1]` + port): requests with any other Host are rejected, which defeats DNS rebinding.
  - **Origin check** on every state-changing request and on WebSocket upgrades; **no CORS** headers at all.
  - Session cookie `HttpOnly; SameSite=Strict`, plus a **CSRF token** on every non-GET request.
  - **Content-Security-Policy** `default-src 'self'`; no inline scripts; artifact previews served from a separate sandboxed origin/iframe with no network.
  - The one-time login link is printed by `cairn web` (random token, single use, short expiry).
- API token file (`~/.local/state/cairn/token`, mode `0600`; per-user ACL on Windows).
- **Contract:** the API is described by an **OpenAPI** document (generated by FastAPI, committed as `api/openapi.json`). Both clients are built and tested against it, and changes that break the contract fail CI. The API is versioned (`/v1`).

---

## 4. Components

### 4.0 Hardware budget and platforms (D21)
Target: **32 GB RAM, no GPU**. Approximate resident memory (Q4_K_M-class quantisation):

| Item | RAM | Loaded |
|---|---|---|
| Qwen3-30B-A3B (main) | ~18–19 GB | when used; kept warm |
| KV cache, 32K context (main) | ~3 GB (fp16; ~1.6 GB at q8_0) | with main |
| Qwen3-4B (fast) | ~2.6 GB | kept warm |
| Qwen3-Embedding-0.6B | ~0.6 GB | while indexing/searching |
| Qwen3-VL-4B/8B (vision) | ~3–6 GB | **on demand only**: the fast model is unloaded first if needed |
| OS + cairn + UI + sandbox | ~4–5 GB | always |

So main + fast + embeddings fit together (~25 GB), and vision is loaded on demand. **Qwen3-VL-30B does not fit alongside the main model**, so it is not a default.
Above 32K context, the KV cache and the slower prompt reading on CPU become the limit; the UI warns when a requested context size would cause swapping to disk.

**Platforms**
- **Linux (Debian 12+, Ubuntu 22.04+, Mint, Pop!_OS):** first-class. Packages: `pipx`/`uv tool install cairn`; optional `.deb` later. Sandbox: rootless Podman.
- **Windows 10/11:** supported. Core and TUI run natively (Windows Terminal); sandbox tiers as in §4.10.1. Paths, keychain (Windows Credential Manager via `keyring`) and the Unix-socket → named-pipe/loopback fallback are abstracted.
- **macOS:** not a target, but nothing should block it.
- CI matrix: Debian 12, Ubuntu 24.04, Windows Server 2022.

### 4.1 Model backends (D5)
- `Backend` interface: `chat(messages, tools, params, stream)`, `embed(texts)`, `list_models()`, `tokenize(text)` (if available), `health()`.
- Drivers: `openai_compat` (llama.cpp, LocalAI, llamafile, vLLM, SGLang, KoboldCpp, ...) and `ollama` (native: list/pull, keep-alive control).
- Tool calling: use native tool calling where the server supports it; otherwise fall back to a constrained-JSON prompt format (Qwen3 tool format).

**Swapping backends and models (D19)**
Backends are named entries in `backends.toml`; anything that uses a model refers to a backend by name, so swapping means editing one line. No code changes.

```toml
[backends.local-ollama]
driver = "ollama"
url = "http://127.0.0.1:11434"

[backends.local-llamacpp]
driver = "openai_compat"
url = "http://127.0.0.1:8080/v1"

[backends.lab-gpu]                 # e.g. a vLLM box on your LAN, later
driver = "openai_compat"
url = "http://10.0.0.5:8000/v1"
location = "lan"                   # local | lan | hosted

[backends.hosted-qwen]             # Alibaba Model Studio (D29); DeepSeek API / OpenRouter are configured the same way
driver = "openai_compat"
url = "https://example-provider/v1"
location = "hosted"
api_key = "keyring:hosted-qwen"    # stored in OS keychain, never in the file
```

A model is chosen at several levels; the most specific one wins: **sub-agent / workflow step → chat → environment → global default**, separately for each role (main, fast, vision, embed). Examples:
- Hardware upgrade: point `main` at `local-llamacpp` with a bigger model; nothing else changes.
- Offloading sub-agents: `spawn_agent(..., backend="hosted-qwen")` or `[workflows.lit-review.steps.extract] backend = "lab-gpu"`.
- Changing models: `m` (model picker) per chat, or edit the environment.

**Rules for non-local backends** (`location = lan | hosted`):
- Treated like any other outbound traffic: the host has to be allowed in the environment's network policy, the payload is **redacted** (§5.4), and every call is audited and shows in the egress report.
- The UI marks a chat that has used a hosted model with a ☁ badge, and the audit log records which messages left the machine.
- Policy option `hosted_backends = "deny" | "ask" | "allow"` (default **deny**); a sub-agent can only use a hosted backend if its parent's environment allows it.
- Locked chats (§5.5) **never** go to hosted backends unless the user overrides this per chat.
- Providers documented and tested first (D29): **Alibaba Model Studio**, **DeepSeek API** and **OpenRouter**, all through the `openai_compat` driver. A dedicated `anthropic` driver (Claude API with a separate API key) is planned for later; Claude subscriptions can't be used as a backend.
- Backends are health-checked on start. If a backend is down, the core falls back to the next configured local backend (`fallback = ["local-ollama"]`) and logs it; it never falls back *to* a hosted one silently.

**Supported servers and licenses** (to verify at implementation time)

| Server | License | Role |
|---|---|---|
| Ollama | MIT | Default (ease of use) |
| llama.cpp `llama-server` | MIT | Recommended for CPU/MoE offload (`--n-cpu-moe`, `-ot`, KV-cache reuse) |
| ik_llama.cpp | MIT | Optional faster CPU/hybrid fork for MoE models |
| llamafile | Apache-2.0 | Optional |
| LocalAI | MIT | Optional |
| vLLM / SGLang | Apache-2.0 | Future GPU / parallel multi-agent |
| KoboldCpp, text-generation-webui | AGPL-3.0 | Works via API; not bundled |
| LM Studio | Proprietary app | Works via API but **not recommended** (not open source) |

**Default model roles** (configurable per environment)

| Role | Default | License |
|---|---|---|
| Main (high/medium effort) | Qwen3-30B-A3B (`qwen3:30b`; 2507 Instruct/Thinking variants if available) | Apache-2.0 |
| Fast (low effort, routing, titles, compaction) | Qwen3-4B or Qwen3-8B | Apache-2.0 |
| Vision (image uploads) | Qwen3-VL-4B (8B if RAM allows), loaded on demand; see §4.0 | Apache-2.0 |
| Embeddings | Qwen3-Embedding-0.6B (or nomic-embed-text) | Apache-2.0 |
| Optional alternative main | DeepSeek models (e.g. distilled R1 / V-series small variants) | MIT |

### 4.2 Effort selector (D12)

| Level | Model | Thinking | Max tool steps | Sub-agents | Notes |
|---|---|---|---|---|---|
| Low | fast model | off (`/no_think`) | 3 | no | Quick answers, renames, summaries |
| Medium (default) | main model | off or short budget | 10 | up to 2, run one after another | |
| High | main model (thinking variant) | on, large budget | 25 | allowed, delegation encouraged | Literature reviews, refactors |

The three levels above are **defaults, not a fixed set** (D20). Effort levels are defined in `effort.toml` (global) and can be overridden or extended per environment. Any number of levels are allowed; `E` cycles through them and the picker lists them all.

```toml
[effort.low]
model_role = "fast"
thinking = false
max_tool_steps = 3
subagents = 0

[effort.deep-review]                 # user-defined
model = "qwen3:30b"                  # explicit model, or model_role
backend = "local-llamacpp"
thinking = true
thinking_budget_tokens = 8192
max_tool_steps = 40
subagents = { max = 6, max_parallel = 1, backend = "local-ollama", effort = "medium" }
context_tokens = 49152
temperature = 0.6
wall_clock_limit = "30m"
skills = ["lit-review", "citation-check"]   # auto-attach
```

Parameters a level can set: model / model role, backend, thinking on/off and budget, sampling (temperature, top_p, top_k, min_p), context size, maximum tool steps, sub-agent limits and their effort, time limit, skills to auto-attach, and whether approvals can be batched. Levels can be reordered and hidden. The effort used for each message is stored with it and shown in the audit log.

### 4.3 Environments (D13)
An environment is a named profile (`~/.config/cairn/environments/<name>.toml`):

```toml
name = "litreview-offline"
workspace = "~/research/thesis"        # FS gateway allowlist root(s)
models = { main = "qwen3:30b", fast = "qwen3:4b", vision = "qwen3-vl:4b" }
backend = "local-ollama"                # any name from backends.toml (§4.1)
effort_default = "high"
context_tokens = 32768
network = { mode = "off", allow_hosts = [] }  # off | ask | allowlist
tools = ["fs.read", "rag.search", "zotero.read", "pdf.read"]
skills = ["lit-review", "citation-check"]
connectors = ["drive:Thesis-Papers", "zotero:local"]
sandbox = { image = "cairn/python:3.12", network = "none", cpu = 4, mem = "4g" }
redaction = { secrets = "block", pii = "ask" }
hosted_backends = "deny"               # deny | ask | allow (§4.1)
encryption = { new_chats_locked = false }  # user-controlled only (§5.5)
```

Built-in templates: `coding`, `litreview-offline`, `litreview-online`, `scratch`.
The environment can be switched per chat; switching is logged.

### 4.4 Context management (D11)
- Default 32K, adjustable per environment/chat, up to what the model supports.
- **Context meter** in both UIs: total used / limit, broken down by system prompt, tools, history, attachments and retrieved chunks.
- **Auto-compaction** at 80%: the fast model summarises older turns; the originals stay in the store and remain searchable.
- Large attachments are never pasted in whole: they are chunked, embedded and retrieved as needed (`rag.search`).
- Prompts are kept stable (system prompt + tools first) so llama.cpp/Ollama can reuse the KV cache, which matters on CPU.

### 4.5 Chats, folders, search
- Chats have: title (auto-generated by the fast model), folder, environment, pinned, archived, timestamps.
- Folders can be nested (one level in MVP); chats can be moved between folders.
- Full-text search across chats (SQLite FTS5; locked chats are searchable only while unlocked).
- Deleting a chat is a **soft delete** with a 30-day trash, then purge. Purging **destroys the chat's key** (crypto-shredding, D36, §5.5), so its encrypted records can't be recovered even from SSD remnants or old backups. The audit log keeps a placeholder entry so the hash chain still verifies.
- Limitation (stated in the UI): for chats that aren't locked, the full-text search index and embeddings are plaintext in SQLite. Purging deletes them with `PRAGMA secure_delete=ON` and a later `VACUUM`, which is best-effort on SSDs. Locked chats or full-disk encryption give a hard guarantee.

### 4.6 Keyboard shortcuts (same in TUI and web)

| Key | Action | Key | Action |
|---|---|---|---|
| `j` / `k` | Move down/up in list | `Enter` | Open chat |
| `n` | New chat | `Esc` | Back / focus list |
| `d` | Delete chat (confirm `y`; `u` to undo) | `/` | Search |
| `f` | Move to folder | `F` | New folder |
| `r` | Rename | `p` | Pin / unpin |
| `a` | Archive | `e` | Environment picker |
| `E` | Cycle effort | `m` | Model picker |
| `u` | Upload file/image | `A` | Open artifacts panel |
| `l` | Audit log for current chat | `x` | Egress report |
| `L` / `Shift+L` | Lock chat / lock its artifacts | `b` | Backend picker |
| `S` | Sync now | `Shift+S` | Sync jobs and status |
| `Ctrl+K` | Command palette | `?` | Help overlay |

List shortcuts only work when the list has focus, never while typing in the message box. Keys can be rebound in `keybindings.toml`.

**Accessibility (D39)**
- `keybindings.single_key = true` (default) can be set to `false`, which makes every single-character shortcut require a modifier (`Alt+d`, ...), as WCAG 2.1.4 requires for screen-reader and speech-input users.
- Web UI targets **WCAG 2.2 AA**: full keyboard operation, visible focus, ARIA roles for the chat list and message log (`role="log"`, polite live region for streaming), contrast ≥ 4.5:1, honours `prefers-reduced-motion`.
- TUI: no colour-only state (badges also have text: `[LOCKED]`, `[CLOUD]`, `[UNSANDBOXED]`), high-contrast theme.

### 4.7 Uploads (files and images)
- Supported: text/code, PDF, DOCX, Markdown, CSV, images (PNG/JPG/WebP).
- Uploads are copied into the store (encrypted if the chat is locked), and their hash is recorded in the audit log.
- PDFs/DOCX are parsed with **Docling** (MIT), with **pypdfium2** (Apache-2.0/BSD) as fallback. **PyMuPDF is avoided (AGPL).**
- Images are sent to the vision model; if no vision model is configured, the UI says so instead of failing silently.

### 4.8 Artifacts (D16)
- Each chat writes files to `<artifacts_root>/<folder>/<chat-slug>/`, which is a **git repository** (D22). Every artifact write the agent makes is one commit (author `cairn-agent`, message = short description + chat/message id + audit sequence number). Edits the user makes outside cairn are picked up and committed as `user` the next time the folder is opened.
- The UI shows history, diffs between versions and restore (restoring is itself a new commit, so history is never rewritten).
- Repos are local only and have no remote by default; adding a remote and pushing is an explicit, audited action.
- Moving a chat to another folder moves its artifact repo; deleting a chat moves the repo into the trash with the chat.
- Types: markdown, code, HTML, SVG/charts, `.bib`, CSV.
- The web UI previews HTML/SVG in a sandboxed iframe with no network (`sandbox` attribute + strict CSP). The TUI renders markdown and opens other types in `$EDITOR` or the system viewer.
- Artifact writes go through the FS gateway and are audited.

### 4.9 Tools, skills, MCP (D15)
- **Tools** come from MCP servers (stdio). Built-in first-party servers: `fs`, `shell` (sandboxed), `git`, `rag`, `pdf`, `zotero`, `scholar` (arXiv/S2/OpenAlex/PubMed/Crossref), `web` (search + fetch).
- Each MCP server has a **manifest** declaring the permissions it needs (`fs:read:<path>`, `net:<host>`, `exec`). The policy engine grants only what both the manifest and the environment allow.
- **MCP trust (D38)**, against tool poisoning (hidden instructions in tool descriptions) and rug pulls (a server changing its tools after approval):
  - Third-party servers are installed at a **pinned version with a package hash**; upgrades are explicit and show a diff of the tool list.
  - When a server is approved, Cairn stores a hash of each tool's **name + description + input schema**. If any of these changes at runtime, the tool is **disabled** until the user re-approves it (the diff is shown).
  - Tool descriptions are shown to the user in full at approval, and are scanned for instruction-like text ("ignore previous", "send", URLs) with a warning.
  - MCP servers run inside the sandbox tier (§4.10.1) with only their declared permissions; their network goes through the egress proxy.
  - Tool results are treated as **untrusted content** (tagged), the same as web pages.
- **Skills** are folders containing `SKILL.md` (frontmatter: name, description, required tools/permissions) plus optional scripts/templates, in the same layout Claude uses so skills can be shared. Skill scripts run in the sandbox.
- Starter skills: `lit-review` (search → screen → extract → synthesise), `citation-check` (checks that every cited source exists via DOI/Crossref), `code-review`, `test-and-fix`.

### 4.10 Coding (D6)
- The workspace repo is mounted **read-only** into the sandbox; the agent proposes **diffs**, the user approves (per hunk or per file), and the core applies them on the host via the FS gateway.
- Commands/tests run in a **Podman** (Apache-2.0, rootless) container with `--network=none` by default; Docker is supported as a fallback. CPU, memory and time limits come from the environment.

#### 4.10.1 Sandbox tiers (D28)
The sandbox runner picks the strongest tier available and shows the active tier in the status bar.

| Tier | Platform | Mechanism | Blocks files outside workspace | Blocks network | Default |
|---|---|---|---|---|---|
| 1 Container | Linux, Windows | Podman (rootless) / Docker | Yes | Yes | Linux default; used on Windows if installed |
| 2 WSL2 + bubblewrap | Windows | Command runs inside a Debian WSL distro under `bwrap` (bubblewrap, LGPL-2.0+, separate process) with no network namespace and only the workspace mounted | Yes | Yes | **Windows default** |
| 2b bubblewrap | Linux | Same as tier 2, natively, when Podman isn't installed | Yes | Yes | Linux fallback |
| 3 Windows Sandbox | Windows Pro/Enterprise | Throwaway built-in VM via `.wsb` config, workspace mapped in, networking off | Yes | Yes | Used if tiers 1–2 are missing |
| 4 Restricted host | Windows, Linux | Plain process: clean environment, a copy of the workspace, CPU/memory/time limits (Job Objects / `prlimit`) | **No** | **No** (unless a firewall rule is configured) | **Off**; enabled per environment with `sandbox.allow_unsandboxed = true` |

Tier 4 rules: every command needs approval (no "allow for this chat"), the UI shows a red **UNSANDBOXED** badge, the audit record is marked `sandbox_tier=4`, and package installs (`pip`, `npm`, ...) show an extra warning. Workflows and sub-agents can never run commands in tier 4.
- Git: diffs, commit with an approval prompt; push only if the environment allows network to the git host **and** the user confirms.

### 4.11 Literature review (D7)
- **Local PDFs:** watch folder → parse → chunk → embed → index (sqlite-vec; encrypted rows if the chat/folder is locked).
- **Zotero:** read via Zotero 7's local API (`localhost:23119`) or a read-only copy of `zotero.sqlite`, plus attached PDFs; citation keys work with Better BibTeX if installed.
- **Online (opt-in):** arXiv, Semantic Scholar, OpenAlex, PubMed (E-utilities), Crossref. Results are cached locally; every query is audited and appears in the egress report.
- Output: structured notes per paper (claims, methods, datasets, limitations), a comparison table, a synthesis draft with **citations only to papers that are actually indexed or resolved**, and a `.bib` artifact.
- Hallucination guard: `citation-check` runs automatically on any synthesis artifact.

**Citation styles (D25)**
- Formatting is **deterministic code, not the model**: a `cite.format` tool uses **citeproc-py** (BSD) with **CSL** style files (Citation Style Language, the standard used by Zotero/Mendeley/Pandoc).
- Shipped styles: `apa` (APA 7th) and `ieee`. Adding a style = dropping a `.csl` file into `~/.config/cairn/csl/` (thousands are available from the open CSL styles repository, CC BY-SA). No code changes.
- A **`references` skill** wraps the tool: it tells the agent when to collect metadata (DOI → Crossref, Zotero item, BibTeX), how to build the bibliography, in-text citation conventions per style, and to run `citation-check` first. So the answer to "is this a skill?" is: **both**. The formatter is a tool (so output is exact and repeatable), and the skill holds the know-how and the default style. Each environment sets a default style (`citation_style = "apa"`); it can be changed per chat or per request ("use IEEE").
- Output formats: in-text + reference list in Markdown/DOCX artifacts, plus a `.bib`/CSL-JSON file.

### 4.12 Connectors and sync (D14, D27, D30)

#### 4.12.1 Sync modes (apply to every sync job)
| Mode | Behaviour |
|---|---|
| `none` (default) | Never syncs. The job is configured but inactive |
| `on_demand` | Only when the user runs `cairn sync [job]`, presses "Sync now" (`S`), or asks the agent (which needs approval) |
| `auto` | Automatically: `on_change` (debounced, e.g. 30s after the last edit) and/or `schedule` (cron, e.g. hourly or nightly); runs only while the core is running, and missed runs catch up at the next start |

```toml
[sync.jobs.thesis-papers]          # incoming: Drive → local mirror
kind = "drive_mirror"
source = "drive:Thesis/Papers"
mode = "on_demand"

[sync.jobs.nightly-backup]         # backup: restic snapshots (§4.12.3)
kind = "backup"
include = ["chats", "artifacts"]   # either or both
target = "rclone:gdrive:Cairn-Backup"   # or "folder:/mnt/nas/cairn-backup" or "sftp:nas:/backup"
mode = "auto"
schedule = "0 2 * * *"             # nightly 02:00
retention = { daily = 7, weekly = 4, monthly = 12 }

[sync.jobs.laptop]                 # replicate: same chats on several machines
kind = "replicate"
include = ["chats", "artifacts"]
scope = ["folder:Thesis", "folder:Coding/*"]   # or "all"
target = "folder:~/Syncthing/cairn"   # or "rclone:gdrive:Cairn-Sync" or "git:git@github.com:me/cairn-data.git"
mode = "auto"
on_change = true
```

Every sync run is audited (job, direction, items, bytes, hashes), and outgoing runs appear in the egress report. Syncing to Drive or a git host counts as outbound network: the host has to be allowed in the environment, and the first run of a new target needs approval.

#### 4.12.2 Incoming connectors (sync to local first)
- **Google Drive:** a sync worker (rclone, MIT, or the Drive API with read-only scope) mirrors **chosen folders** into the local workspace store according to the job's mode. Before applying, on-demand runs show a preview (new/changed/deleted files); automatic runs apply and log. The agent only sees the local mirror. OAuth tokens are stored in the OS keychain (`keyring`).
- **GitHub/Git:** repos are cloned locally; `git fetch` follows the job's mode. The agent works against the local clone.

#### 4.12.3 Chat and artifact sync (D30, D40)
Two different needs, each handled by a proven tool rather than home-grown merge code ([ADR-0007](adr/0007-sync-restic-backup-and-event-log-replication.md)):

| Need | Job kind | Mechanism | Transport |
|---|---|---|---|
| **Backup / restore** | `backup` | **restic** (BSD-2-Clause) snapshots of the Cairn data folder: deduplicated, versioned, `cairn restore --at <date>` | local folder/NAS, SFTP, or Google Drive / other clouds via restic's rclone backend |
| **Same chats on several machines** | `replicate` | **Per-device append-only event logs** for chats; **git** for artifacts | any shared folder (local/NAS/Syncthing), Google Drive (via rclone), or a private git remote |

**Backup (`backup`)**
- restic repositories are **always encrypted** (that's how restic works). The repo password is set by the user when they create the job and kept in the OS keychain; this counts as the user opting in. Losing it means losing the backup; setup says so and offers to export it.
- Retention policy per job (e.g. keep 7 daily, 4 weekly, 12 monthly), `cairn backup check` runs restic's integrity check.

**Replicate (`replicate`)**
- **Chats** are stored as **events** (chat created, message appended, renamed, moved, effort/environment changed, locked, deleted/tombstone). Each device writes **only its own log files** (`devices/<device-id>/events-<n>.jsonl`) and only reads the others', so two devices never write the same file and file-level sync conflicts can't happen.
- Each device rebuilds its local state by merging all logs in order (Lamport clock, then device ID). This is event sourcing: the merge is deterministic and every device ends up with the same state.
- **Concurrent turns:** if two devices append a message to the same parent message, the chat **forks** into two branches ("Thesis notes" and "Thesis notes (laptop)"), shown side by side; nothing is mixed.
- Messages are never edited in place, only appended; renames/moves use last-writer-wins by the same ordering.
- **Artifacts** are each chat's git repo, pushed/pulled to a bare repo on the target, so history comes along and conflicts are normal git merges (if a merge doesn't apply cleanly, the remote version is kept as a branch and the user is asked).
- **Folders** and settings that aren't secrets (environments, effort levels, skills list) are replicated as events too. **Never synced:** API keys, OAuth tokens, passphrases, chat keys of unlocked chats, the local audit-log signing key.
- Log files are compacted (snapshot + tail) when they grow; compaction is itself an event.
- If true collaborative editing of a document is needed later, a CRDT library (Automerge, MIT) is the planned approach; it isn't needed for append-only chats.

**Common rules**
- **Audit across devices:** each machine keeps its own hash-chained audit log; sync records reference the remote device ID, and `cairn audit verify --all-devices` checks every synced device log.
- **Encryption of synced data (D31):** in `replicate` jobs, events and artifacts of items the user has locked (§5.5) are written **only as age-encrypted data** (encrypted on this machine; the target never sees plaintext or keys). Unlocked items are written as plain JSON/git. When the target is a cloud service and the scope contains unlocked items, setup shows a clear warning, offers "encrypt this sync target" (a target passphrase; every event/object written there is encrypted), and shows a ☁ badge on synced chats. Encryption stays opt-in; the warning is a prompt, not a requirement.
- The agent can *start* a sync (with approval) but cannot create or change sync jobs, targets or modes; only the user can.

### 4.13 Web search
- Default backend: self-hosted **SearXNG** (AGPL-3.0, run as a separate container, not bundled into cairn).
- Optional: other search APIs via plugin, all through the egress proxy.
- `web.fetch` pulls readable text; pages are treated as **untrusted content** (prompt-injection guard: content goes in tagged blocks, and tools that follow links need approval).

### 4.14 Multi-agent orchestration (D10)
- **Delegation:** the main agent has a `spawn_agent(task, tools, effort, budget, backend?)` tool (backend rules: §4.1). A sub-agent gets its own context, **a subset** of the parent's permissions, and returns a summary plus artifacts. Its activity is nested under the parent in the audit log.
- **Workflows:** YAML pipelines in `workflows/` (e.g. `lit-review.yaml`: `search → dedupe → screen → extract(per paper, delegated) → synthesise → citation-check`). Steps may call delegation.
- **CPU reality:** sub-agents run one at a time by default (`max_parallel = 1`); the queue is shown in the UI. Parallelism can be raised per backend (`max_parallel` in `backends.toml`), e.g. for a hosted or GPU backend, while local CPU stays at 1.
- Hard limits: maximum depth (2), total token budget, total tool steps, wall-clock time.

---

## 5. Security and privacy (D4, D8)

### 5.1 Threat model (summary; full version in [threat-model.md](threat-model.md))
- Method (D33): **STRIDE** per component and data flow for security, **LINDDUN** for privacy, and every control mapped to the **OWASP Top 10 for LLM Applications (2025)** (LLM01 Prompt Injection … LLM10 Unbounded Consumption). Each threat has an ID (`T-xx`), its controls, and the requirement/test that checks it.
- **Protects against:** data leaving the machine without consent; the agent damaging files outside the workspace; prompt injection from web pages, PDFs, repos or tool output causing data to leak or harmful actions; malicious web pages attacking the local API; poisoned or changed MCP tools; local tampering with the action history going unnoticed; theft of the data directory or disk (locked chats; full-disk encryption recommended for the rest).
- **Privacy note (LINDDUN):** the audit log itself is sensitive (it records what you did, when). It holds digests and metadata rather than content, has a configurable retention period, and is never synced to cloud targets unless the user includes it.
- **Out of scope v1:** a compromised OS/root user; malicious model weights (mitigated by GGUF/safetensors-only formats and checksums); side channels.

### 5.2 Tamper-evident audit log
- Append-only table in SQLite + mirrored JSONL file.
- Record: `{seq, ts, chat_id, agent_id, parent_agent_id, actor, action, target, policy_decision, params_digest, result_digest, prev_hash, hash}` where `hash = SHA-256(prev_hash ‖ canonical_json(record))`.
- Full prompts/outputs are stored in the chat store (encrypted if the chat is locked) and **referenced by digest**, so the log can be shared without exposing content.
- `cairn audit verify` recomputes the chain. Checkpoints are signed with a local Ed25519 key, and the user can export a checkpoint (e.g. print or store it elsewhere) to detect a rewritten history.
- UI: per-chat audit view (`l`), a global audit browser with filters, and export (JSONL/CSV).
- **Tracing export (D37):** optional export of model and tool calls as **OpenTelemetry** spans using the GenAI semantic conventions (`gen_ai.*` attributes: model, token counts, operation), to a local file or a local collector (e.g. for Grafana/Jaeger). Off by default; content fields are excluded unless the user includes them; a remote collector counts as egress.
- **Phase 3:** Merkle-tree checkpoints (as used in Certificate Transparency / Sigstore Rekor) for efficient proofs that a record is in the log.
- Retention: configurable (default: keep forever); purging old entries keeps a signed checkpoint so the remaining chain still verifies.

### 5.3 Policy engine
- Decides `allow | ask | deny` from environment rules + tool manifest + action type.
- Defaults: reads inside the workspace are allowed; writes **ask**; exec runs in the sandbox only; network is **denied** unless the environment opts in, and then it's allowlisted or asked per host.
- "Ask" prompts show the exact action (diff, command, URL + redacted payload). Approvals can be scoped to: once / this chat / this environment.

### 5.4 Outbound redaction
- All egress goes through the core's proxy. Before sending: secret scan (gitleaks-style rule set) + personal-data scan (Microsoft **Presidio**, MIT) + custom regexes from the environment.
- Action per category: `block | ask | mask | allow`. Defaults: secrets **block**, personal data **ask**.
- What was redacted is recorded in the audit log (category + digest, not the value).

### 5.5 Encryption (D23, D24): user-controlled, off by default
- **Default: no encryption.** No passphrase at start; everything opens immediately. First run recommends full-disk encryption (LUKS on Linux, BitLocker on Windows) and links to how to set it up, without enforcing it.
- **Only the user can turn encryption on**, in either of two ways:
  1. Directly: `cairn lock <chat>`, the chat menu ("Lock chat…"), or a shortcut (`L`).
  2. By explicitly asking the agent ("encrypt this chat", "lock the thesis folder"). The agent calls the `encrypt` tool, which **always** ends in a confirmation dialog in the client, where the user types the passphrase. The passphrase goes from the client to the core's crypto module and **never enters the model context**, tool arguments, logs or the audit record.
  The `encrypt`/`decrypt` tools are marked `user_initiated_only`: the policy engine rejects them unless the message that triggered the call is a user message in the same chat (not tool output, web content, a file or a sub-agent). So prompt injection can't trigger encryption (e.g. a ransomware-style "encrypt everything" request).
- **Key hierarchy (D35, D36)**, using the reviewed **age** format (via pyrage, MIT/Apache-2.0) rather than a custom container:
  - Every chat has its own random **chat key** (an age X25519 identity). All of the chat's records in the store (messages, attachments) are encrypted with it, **whether or not the chat is locked**.
  - **Unlocked chat:** the chat key is kept in the local key store without a passphrase, so nothing is asked and opening is instant. This is not protection against someone with access to your disk (that's what locking or full-disk encryption is for); it exists so deletion is reliable: **purging a chat destroys its key** (crypto-shredding).
  - **Locked chat:** the chat key is **wrapped** (encrypted) with the passphrase using age's scrypt recipient, and optionally also with a recovery key. Changing the passphrase only re-wraps the key; the chat's data isn't re-encrypted.
  - Locked artifacts use their own artifact key, wrapped the same way.
  - Synced and backed-up copies of locked items contain only wrapped keys.
- **Passphrases only for locked chats (D24):** each locked chat (or folder) has its own wrapped key; unlocked chats need no passphrase. Locked chats show 🔒 in the list; opening one asks for its passphrase; it re-locks after a configurable idle time. One passphrase can optionally be reused across several chats.
- **Chats and artifacts are opt-in separately (D31):**
  - *Lock chat* encrypts messages, attachments, its search-index and embedding rows, and memory items from it.
  - *Lock artifacts* encrypts the chat's artifact repo (stored as an encrypted git bundle while locked, unpacked into a temporary folder while unlocked and re-packed on lock).
  - Either one can be applied to a single chat, a whole folder (new chats inside inherit it), or a sync target (§4.12.3). `L` locks the chat, `Shift+L` locks its artifacts; the lock dialog offers "both".
  - Synced copies of locked items are always encrypted before upload; keys and passphrases never leave the machine.
- The audit log stays readable and verifiable without passphrases: locked-chat entries hold only digests and metadata (time, tool, host, bytes), never content.
- **Removing encryption** (`unlock --permanent`) also requires the passphrase and is user-initiated only.
- **Lost passphrase = lost data.** The lock dialog says so; an optional recovery key can be exported at lock time.
- Implementation: age (pyrage) for chat records, attachments, wrapped keys and encrypted sync data; the SQLite database holds metadata, the search index and encrypted blobs. Optional global "encrypt everything" mode (SQLCipher) for later (Phase 3).

### 5.6 Per-chat egress report
- Panel (`x`) listing every outbound request for the chat: time, host, tool, bytes in/out, what was redacted, decision, who approved. Exportable.
- Global "network today" summary in the status bar (e.g. `⇅ 0 hosts` offline badge).

### 5.7 Supply chain (D41)
- **Dependencies:** `uv` lockfile with hashes; minimal core dependencies; `pip-audit` (known vulnerabilities) and a license check (block AGPL/GPL in the core package) in CI; Dependabot for Python and GitHub Actions updates.
- **CI hardening:** GitHub Actions pinned to commit SHAs, least-privilege `permissions:`, no secrets in pull-request jobs; **OpenSSF Scorecard** runs weekly.
- **Releases:** a **CycloneDX SBOM** (software bill of materials) per release; wheels and installers **signed with Sigstore** (keyless, via GitHub OIDC) with build provenance attestations (SLSA build level 2+); `cairn self-verify` checks an install against them.
- **Models:** only **GGUF** and **safetensors** files are accepted (never pickle-based `.bin`/`.pt`, which can run code when loaded); SHA-256 checked against the source (Hugging Face / Ollama manifest) on download and recorded in the audit log.
- **MCP servers / skills** from third parties are installed **disabled** and pinned (§4.9); enabling one shows the permissions its manifest requests.

### 5.8 Policy for `encrypt`, sync jobs and other user-only actions
Actions marked `user_initiated_only` (encrypt/decrypt, creating or changing sync jobs, enabling hosted backends, enabling tier-4 sandbox, approving MCP tools) can only be triggered by a user message in the same chat or by the UI itself, never by tool output, files, web content or sub-agents. This is the main defence against *excessive agency* (OWASP LLM06).

---

## 6. Technology choices

| Area | Choice | License |
|---|---|---|
| Language | Python 3.12+ (Rust later for audit/egress) | — |
| Packaging | uv | MIT/Apache-2.0 |
| Core API | FastAPI + Uvicorn, WebSocket streaming | MIT / BSD |
| TUI | Textual | MIT |
| Web UI | Svelte (served by the core, built offline) | MIT |
| DB | SQLite, FTS5, sqlite-vec (SQLCipher optional, Phase 3) | Public domain / MIT-Apache / BSD-style |
| Crypto | age format via pyrage | MIT / Apache-2.0 |
| Config | pydantic models → JSON Schema (`schema_version` in every file) | MIT |
| DB migrations | Alembic (+ SQLAlchemy Core) | MIT |
| Backup | restic (external binary) | BSD-2-Clause |
| Tracing (optional) | OpenTelemetry SDK | Apache-2.0 |
| Supply chain | cyclonedx-py, Sigstore, pip-audit, OpenSSF Scorecard | Apache-2.0 |
| Artifact versioning | git (via dulwich or the git CLI) | Apache-2.0/GPL-2.0 (dulwich) / GPL-2.0 (CLI, separate process) |
| Citations | citeproc-py + CSL styles | BSD / CC BY-SA (styles) |
| Tools protocol | MCP Python SDK | MIT |
| Sandbox | Podman (Docker fallback) | Apache-2.0 |
| Parsing | Docling, pypdfium2 | MIT, Apache-2.0/BSD |
| Redaction | Presidio + secret rules | MIT |
| Search | SearXNG (external) | AGPL-3.0 (separate process) |
| Drive mirror and cloud sync transport | rclone (external binary) | MIT |
| Zotero | pyzotero / local API | MIT |
| Secrets | keyring | MIT |

---

## 7. Delivery plan

### Phase 0 — Skeleton (thin slice of everything, D17)
Goal: every feature exists end to end in a minimal form. Delivered as a **walking skeleton** (M0) followed by milestones M1–M3 that each add features end to end; see [plan/phase0.md](plan/phase0.md). Requirement IDs and priorities are in [requirements.md](requirements.md); "Must" items define Phase 0 done, "Should" items may slip to a Phase 0.x point release.

| Feature | Thin-slice scope | Acceptance criterion |
|---|---|---|
| Core daemon + API | start/stop, auth token, health | `cairn serve` + `cairn status` work; API is only reachable locally |
| Backends | Ollama + OpenAI-compat drivers, `backends.toml`, per-role selection | Streams a reply from `qwen3:30b` via Ollama **and** via llama-server; switching backends is one config line; hosted backend is denied by default |
| TUI | chat list, chat view, input, sidebar | All §4.6 shortcuts work; `d` deletes with confirm + undo |
| Web UI | same screens, read/write | Same shortcuts work; works with the network disconnected |
| Folders | create, move, one level | `f` moves chat; folder survives restart |
| Effort | 3 defaults + custom levels from `effort.toml` | Low uses the fast model; High enables thinking; a user-defined level shows up in the picker; recorded in audit |
| Environments | load TOML, picker, 2 templates | Switching env changes tools/network policy, and this is visible in audit |
| Context meter | tokens used / limit + compaction | Meter is within ±5% of the server's token count; compaction triggers at 80% |
| Uploads | text, PDF (Docling), image → vision | A PDF question gets an answer citing page numbers; image described by the vision model |
| Artifacts | git repo per chat + preview | Agent writes `notes.md` twice → 2 commits; restore works; web preview loads in sandboxed iframe |
| Tools/MCP | fs, shell(sandbox), rag | Tool calls audited; shell has no network |
| Skills | loader + 1 skill (`lit-review` stub) | Skill shown in list; permissions it requests are enforced |
| Coding | diff approve + sandbox test run | Agent fixes a failing test in a sample repo with ≤2 approvals |
| Lit review | local PDFs + arXiv search | Produces a table + `.bib` from 5 PDFs; arXiv call appears in egress report |
| Zotero | read collections (local API) | Lists items from a chosen collection |
| Connectors | git clone/fetch; Drive folder mirror | Mirror a Drive folder read-only; agent reads local copy only |
| Sync | modes none / on_demand / auto; `backup` (restic) and `replicate` (event logs + git) to a local folder and to Drive | Two installs replicating via one folder see each other's chats and artifact history; concurrent turns fork; a locked chat arrives encrypted; a backup restores to a given date; a job in mode `none` never runs |
| Web search | SearXNG backend, opt-in | Blocked in offline env; allowed + logged in online env |
| Multi-agent | `spawn_agent` (max_parallel 1) + 1 YAML workflow | Sub-agent appears nested in audit; budget limits enforced |
| Audit log | hash chain + verify + per-chat view | `cairn audit verify` fails after a record is edited by hand |
| Redaction | secret rules + Presidio | A fake AWS key in an outbound query is blocked |
| Encryption | per-chat age keys, lock = wrap with passphrase, user-initiated only | A locked chat can't be read without its passphrase; an `encrypt` call triggered from web/tool content is rejected; passphrase never appears in logs or model context; a purged chat can't be decrypted from a copy of the old database |
| Local API security | Unix socket, Host/Origin/CSRF checks | A request with a foreign Host or Origin header is rejected; a cross-site form post fails |
| Citations | `cite.format` (CSL) + `references` skill | The same references come out correctly in APA and IEEE; adding a `.csl` file adds a style |
| Platforms | Debian + Windows | Phase 0 test suite passes on Debian 12 and Windows 11; sandbox tier 1 on Debian and tier 2 on Windows block network and outside-workspace reads |
| Egress report | per-chat panel | Lists host, bytes and redactions for each request |

### Phase 1 — Depth: coding
Hunk-level approval, git commit flow, LSP-backed code search, test-and-fix skill, repo indexing.

### Phase 2 — Depth: literature review
Semantic Scholar/OpenAlex/PubMed/Crossref, dedupe, screening criteria, extraction schemas, automatic citation-check, Zotero write-back (opt-in).

### Phase 3 — Orchestration and hardening
Workflow editor, parallel sub-agents on GPU backends, signed audit checkpoints, Rust rewrite of audit log + egress proxy (D9), optional encrypt-everything mode (SQLCipher), `.deb` / Windows installer.

---

## 8. Non-functional requirements (summary)
Full list with IDs in [requirements.md](requirements.md). Performance targets marked *provisional* will be fixed from `scripts/bench_backend.py` results on the reference machine (32 GB RAM, CPU only, Debian).

| Area | Target |
|---|---|
| Responsiveness | UI reacts to input ≤ 100 ms; Cairn adds ≤ 50 ms per turn on top of the model (policy + audit + context assembly) |
| Model speed (provisional) | First token ≤ 10 s for a 2K-token prompt on the main model (warm); fast model ≤ 3 s; a warning before any request expected to exceed 60 s of prompt processing |
| Footprint | Cairn processes ≤ 1 GB RAM excluding model servers; core starts ≤ 3 s |
| Reliability | No data loss on crash or power cut (SQLite WAL, atomic file writes); an audit record is written before every effect; interrupted turns can be resumed |
| Offline | Every feature that doesn't inherently need the network works with the network unplugged |
| Accessibility | Web UI WCAG 2.2 AA; single-key shortcuts can be turned off |
| Portability | Debian 12+, Ubuntu 22.04+, Windows 10/11; Python 3.12+ |
| Privacy | Zero outbound connections in a default install, verified by a test that runs Cairn with the network blocked and checks the egress log |

## 9. Testing and quality strategy

| Layer | What | Tooling |
|---|---|---|
| Static | Lint, formatting, strict typing | ruff, mypy (strict) |
| Unit | Policy engine, audit chain, crypto wrapping, config validation, event-log merge | pytest, **Hypothesis** (property-based, e.g. "any interleaving of two device logs merges to the same state") |
| Contract | Core API vs `api/openapi.json`; config files vs JSON Schema | schemathesis (API fuzzing from the OpenAPI document) |
| Integration | Core with a **fake model server** (scripted replies, tool calls) and real SQLite, sandbox, git | pytest + local fake Ollama/OpenAI server |
| End to end | TUI and web UI flows, including every shortcut | Textual's pilot test harness; Playwright for the web UI, with axe-core for accessibility |
| Model evaluations | Golden tasks run against the real local model: tool-call accuracy, diff correctness on sample repos, PDF Q&A with page citations, **citation existence** (no invented references), compaction keeping key facts | small eval sets in `evals/`, run nightly or on demand (too slow for every commit on CPU) |
| Security | **Prompt-injection corpus** (malicious PDFs, web pages, tool outputs, repo files) must not trigger network, writes or user-only actions; DNS-rebinding/CSRF tests against the local API; redaction tests with fake secrets; `audit verify` tamper tests | pytest suites in `tests/security/` |
| Fuzzing | File parsers and import paths (PDF, DOCX, event logs, config) | Atheris / Hypothesis |
| Platforms | Every test layer except model evals runs on Debian 12, Ubuntu 24.04 and Windows | GitHub Actions matrix |

Every Must requirement has at least one automated test; [requirements.md](requirements.md) names it.

## 10. Data and configuration contracts
- **Config:** every TOML file (`backends`, `effort`, `environments/*`, `sync`, `keybindings`) is validated by a pydantic model on load, with a published JSON Schema (editor autocompletion) and a `schema_version`; older files are migrated automatically with a backup kept.
- **Database:** schema changes only via versioned **Alembic** migrations; a backup is taken before migrating; downgrades are supported for one version.
- **API:** OpenAPI document committed and checked in CI (§3.1).
- **Event log format** (replication) and **artifact commit messages** are versioned formats documented in `docs/formats/`.

## 11. Open questions
Resolved in v0.2: hardware (D21), artifact versioning (D22), encryption (D23), unlocking (D24), citation styles (D25).
Resolved in v0.3: name (D26), Drive sync (D27), Windows sandbox (D28), hosted providers (D29).
Resolved in v0.4: sync modes (D27, revised), chat + artifact sync (D30), encryption scope (D31).

Resolved in v0.5: industry-practice alignment (D32–D41).

1. **Drive folders:** which folders to set up for the first sync (can be decided at setup time).
2. **Project license:** Apache-2.0 suggested (patent grant; matches Qwen's license); not yet decided.
3. **Performance targets:** fix the provisional numbers in §8 once `scripts/bench_backend.py` has been run on the reference machine.

## 12. Risks
- **Syncing unencrypted chats to a cloud target exposes their content to that provider.** Mitigated by mode `none` by default, the setup warning, the "encrypt this sync target" option, ☁ badges and egress reporting.
- **Hosted backends weaken privacy.** Mitigated by deny-by-default, redaction, the ☁ badge, locked chats never going to hosted backends, and the egress report.
- **CPU speed:** long prompts and sub-agents are slow. Mitigated by retrieval, compaction, KV-cache reuse, the fast model for routing, and running sub-agents one at a time.
- **Tool-calling reliability** of 30B-class local models. Mitigated by JSON-schema-constrained output (grammar in llama.cpp), small tool sets per environment, and retry/repair.
- **Scope:** "thin slice of everything" covers many features. Each item is kept to its Phase 0 acceptance criterion; anything beyond it moves to later phases.
- **Prompt injection** from PDFs/web pages/tool output. Mitigated by tagging untrusted content, requiring approval for network and write actions, `user_initiated_only` actions (§5.8), the injection test corpus (§9), and the audit trail. No current technique fully prevents prompt injection, so the design limits what a successful injection can do.
- **Crypto-shredding gap** for unlocked chats' search index and embeddings (plaintext in SQLite). Stated in the UI; locked chats or full-disk encryption close it.
- **External binaries** (restic, rclone, Podman, bubblewrap) add install steps, especially on Windows. Mitigated by a `cairn doctor` command that checks and explains what's missing, and by features degrading gracefully (e.g. no restic → backup jobs unavailable, everything else works).
