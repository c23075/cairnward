# Spec: `incant` — a local, privacy-first AI agent

Status: **Draft v0.1, for review**
Date: 2026-10-31
Repo: `c23075/possessed-incantation`
Working name: **incant** (CLI command `incant`; rename freely)

---

## 1. Purpose

A local AI agent for **coding** and **literature-review research**, with a feature
set similar to the Claude app, that runs on open-weight models on the user's own
hardware. **Privacy and security come first**: every action the agent takes is
recorded in a log that shows any later tampering, nothing leaves the machine
unless explicitly allowed, and data on disk is encrypted.

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
| D3 | Hardware | CPU / mixed offload (exact RAM/VRAM: see open questions) |
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
| D18 | Spec location | This repo, `docs/spec.md` |

---

## 3. Architecture

```
 ┌──────────────┐   ┌──────────────┐
 │  TUI client  │   │ Web UI client│        (thin clients: no direct
 │  (Textual)   │   │ (Svelte SPA) │         model, file or network access)
 └──────┬───────┘   └──────┬───────┘
        │  HTTP + WebSocket on a Unix socket / 127.0.0.1, auth token
 ┌──────▼──────────────────▼─────────────────────────────────────────┐
 │                         incant core (daemon)                       │
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
 │  Encrypted store: SQLCipher (chats, memory, audit, vector index)   │
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

---

## 4. Components

### 4.1 Model backends (D5)
- `Backend` interface: `chat(messages, tools, params, stream)`, `embed(texts)`, `list_models()`, `tokenize(text)` (if available), `health()`.
- Drivers: `openai_compat` (llama.cpp, LocalAI, llamafile, vLLM, SGLang, KoboldCpp, ...) and `ollama` (native: list/pull, keep-alive control).
- Tool calling: use native tool calling where the server supports it; otherwise fall back to a constrained-JSON prompt format (Qwen3 tool format).

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
| Vision (image uploads) | Qwen3-VL-30B-A3B or a smaller Qwen-VL model, depending on RAM | Apache-2.0 |
| Embeddings | Qwen3-Embedding-0.6B (or nomic-embed-text) | Apache-2.0 |
| Optional alternative main | DeepSeek models (e.g. distilled R1 / V-series small variants) | MIT |

### 4.2 Effort selector (D12)

| Level | Model | Thinking | Max tool steps | Sub-agents | Notes |
|---|---|---|---|---|---|
| Low | fast model | off (`/no_think`) | 3 | no | Quick answers, renames, summaries |
| Medium (default) | main model | off or short budget | 10 | up to 2, run one after another | |
| High | main model (thinking variant) | on, large budget | 25 | allowed, delegation encouraged | Literature reviews, refactors |

All values are set in the profile. The selected effort is stored with each message and shown in the audit log.

### 4.3 Environments (D13)
An environment is a named profile (`~/.config/incant/environments/<name>.toml`):

```toml
name = "litreview-offline"
workspace = "~/research/thesis"        # FS gateway allowlist root(s)
models = { main = "qwen3:30b", fast = "qwen3:4b", vision = "qwen3-vl:30b" }
backend = "ollama"                      # or "llamacpp-local"
effort_default = "high"
context_tokens = 32768
network = { mode = "off", allow_hosts = [] }  # off | ask | allowlist
tools = ["fs.read", "rag.search", "zotero.read", "pdf.read"]
skills = ["lit-review", "citation-check"]
connectors = ["drive:Thesis-Papers", "zotero:local"]
sandbox = { image = "incant/python:3.12", network = "none", cpu = 4, mem = "4g" }
redaction = { secrets = "block", pii = "ask" }
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
- Full-text search across chats (SQLite FTS5 inside the encrypted DB).
- Deleting a chat is a **soft delete** with a 30-day trash, then secure purge. Purging removes content but keeps a placeholder entry in the audit log so the hash chain still verifies.

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
| `Ctrl+K` | Command palette | `?` | Help overlay |

List shortcuts only work when the list has focus, never while typing in the message box. Keys can be rebound in `keybindings.toml`.

### 4.7 Uploads (files and images)
- Supported: text/code, PDF, DOCX, Markdown, CSV, images (PNG/JPG/WebP).
- Uploads are copied into the encrypted store, and their hash is recorded in the audit log.
- PDFs/DOCX are parsed with **Docling** (MIT), with **pypdfium2** (Apache-2.0/BSD) as fallback. **PyMuPDF is avoided (AGPL).**
- Images are sent to the vision model; if no vision model is configured, the UI says so instead of failing silently.

### 4.8 Artifacts (D16)
- Each chat writes files to `<artifacts_root>/<folder>/<chat-slug>/`, and every write creates a new version (`file.md`, `.versions/file.v3.md`, or a git repo per chat; see open questions).
- Types: markdown, code, HTML, SVG/charts, `.bib`, CSV.
- The web UI previews HTML/SVG in a sandboxed iframe with no network (`sandbox` attribute + strict CSP). The TUI renders markdown and opens other types in `$EDITOR` or the system viewer.
- Artifact writes go through the FS gateway and are audited.

### 4.9 Tools, skills, MCP (D15)
- **Tools** come from MCP servers (stdio). Built-in first-party servers: `fs`, `shell` (sandboxed), `git`, `rag`, `pdf`, `zotero`, `scholar` (arXiv/S2/OpenAlex/PubMed/Crossref), `web` (search + fetch).
- Each MCP server has a **manifest** declaring the permissions it needs (`fs:read:<path>`, `net:<host>`, `exec`). The policy engine grants only what both the manifest and the environment allow.
- **Skills** are folders containing `SKILL.md` (frontmatter: name, description, required tools/permissions) plus optional scripts/templates, in the same layout Claude uses so skills can be shared. Skill scripts run in the sandbox.
- Starter skills: `lit-review` (search → screen → extract → synthesise), `citation-check` (checks that every cited source exists via DOI/Crossref), `code-review`, `test-and-fix`.

### 4.10 Coding (D6)
- The workspace repo is mounted **read-only** into the sandbox; the agent proposes **diffs**, the user approves (per hunk or per file), and the core applies them on the host via the FS gateway.
- Commands/tests run in a **Podman** (Apache-2.0, rootless) container with `--network=none` by default; Docker is supported as a fallback. CPU, memory and time limits come from the environment.
- Git: diffs, commit with an approval prompt; push only if the environment allows network to the git host **and** the user confirms.

### 4.11 Literature review (D7)
- **Local PDFs:** watch folder → parse → chunk → embed → index (sqlite-vec inside SQLCipher DB).
- **Zotero:** read via Zotero 7's local API (`localhost:23119`) or a read-only copy of `zotero.sqlite`, plus attached PDFs; citation keys work with Better BibTeX if installed.
- **Online (opt-in):** arXiv, Semantic Scholar, OpenAlex, PubMed (E-utilities), Crossref. Results are cached locally; every query is audited and appears in the egress report.
- Output: structured notes per paper (claims, methods, datasets, limitations), a comparison table, a synthesis draft with **citations only to papers that are actually indexed or resolved**, and a `.bib` artifact.
- Hallucination guard: `citation-check` runs automatically on any synthesis artifact.

### 4.12 Connectors (D14: sync to local first)
- **Google Drive:** a sync worker (rclone, MIT, or the Drive API with read-only scope) mirrors **chosen folders** into the encrypted workspace store on demand or on a schedule. The agent only sees the local mirror. OAuth tokens are stored in the OS keychain (`keyring`).
- **GitHub/Git:** repos are cloned locally; `git fetch` runs as an explicit, audited sync action. The agent works against the local clone.
- Each sync logs what came in (files, sizes, hashes). Nothing is uploaded unless the user starts a push or upload, which is approved and audited.

### 4.13 Web search
- Default backend: self-hosted **SearXNG** (AGPL-3.0, run as a separate container, not bundled into incant).
- Optional: other search APIs via plugin, all through the egress proxy.
- `web.fetch` pulls readable text; pages are treated as **untrusted content** (prompt-injection guard: content goes in tagged blocks, and tools that follow links need approval).

### 4.14 Multi-agent orchestration (D10)
- **Delegation:** the main agent has a `spawn_agent(task, tools, effort, budget)` tool. A sub-agent gets its own context, **a subset** of the parent's permissions, and returns a summary plus artifacts. Its activity is nested under the parent in the audit log.
- **Workflows:** YAML pipelines in `workflows/` (e.g. `lit-review.yaml`: `search → dedupe → screen → extract(per paper, delegated) → synthesise → citation-check`). Steps may call delegation.
- **CPU reality:** sub-agents run one at a time by default (`max_parallel = 1`); the queue is shown in the UI. Parallelism can be raised later for GPU/vLLM backends.
- Hard limits: maximum depth (2), total token budget, total tool steps, wall-clock time.

---

## 5. Security and privacy (D4, D8)

### 5.1 Threat model (summary)
- **Protects against:** data leaving the machine without consent; the agent damaging files outside the workspace; prompt injection from web pages, PDFs or repos causing data to leak or harmful actions; local tampering with the action history going unnoticed; theft of the data directory or disk.
- **Out of scope v1:** a compromised OS/root user; malicious model weights (mitigated only by verifying checksums); side channels.

### 5.2 Tamper-evident audit log
- Append-only table in SQLCipher + mirrored JSONL file.
- Record: `{seq, ts, chat_id, agent_id, parent_agent_id, actor, action, target, policy_decision, params_digest, result_digest, prev_hash, hash}` where `hash = SHA-256(prev_hash ‖ canonical_json(record))`.
- Full prompts/outputs are stored in the encrypted store and **referenced by digest**, so the log can be shared without exposing content.
- `incant audit verify` recomputes the chain. Checkpoints are signed with a local Ed25519 key, and the user can export a checkpoint (e.g. print or store it elsewhere) to detect a rewritten history.
- UI: per-chat audit view (`l`), a global audit browser with filters, and export (JSONL/CSV).

### 5.3 Policy engine
- Decides `allow | ask | deny` from environment rules + tool manifest + action type.
- Defaults: reads inside the workspace are allowed; writes **ask**; exec runs in the sandbox only; network is **denied** unless the environment opts in, and then it's allowlisted or asked per host.
- "Ask" prompts show the exact action (diff, command, URL + redacted payload). Approvals can be scoped to: once / this chat / this environment.

### 5.4 Outbound redaction
- All egress goes through the core's proxy. Before sending: secret scan (gitleaks-style rule set) + personal-data scan (Microsoft **Presidio**, MIT) + custom regexes from the environment.
- Action per category: `block | ask | mask | allow`. Defaults: secrets **block**, personal data **ask**.
- What was redacted is recorded in the audit log (category + digest, not the value).

### 5.5 Encryption at rest
- Chats, memory, audit log and vector index live in **SQLCipher**. The key comes from a passphrase via Argon2id, or is kept in the OS keychain; the UI shows a locked state.
- Uploads and synced connector data are encrypted (age/XChaCha20) in the store.
- Artifacts are plaintext files by default so editors can open them; an optional encrypted-artifacts mode is available. **Full-disk encryption is recommended** and checked at first run (see open questions).

### 5.6 Per-chat egress report
- Panel (`x`) listing every outbound request for the chat: time, host, tool, bytes in/out, what was redacted, decision, who approved. Exportable.
- Global "network today" summary in the status bar (e.g. `⇅ 0 hosts` offline badge).

### 5.7 Supply chain
- `uv` lockfile with hashes; minimal core dependencies; licenses checked in CI (block AGPL/GPL in the core package).
- Model files verified by checksum on download.
- MCP servers / skills from third parties are installed **disabled**; enabling one shows the permissions its manifest requests.

---

## 6. Technology choices

| Area | Choice | License |
|---|---|---|
| Language | Python 3.12+ (Rust later for audit/egress) | — |
| Packaging | uv | MIT/Apache-2.0 |
| Core API | FastAPI + Uvicorn, WebSocket streaming | MIT / BSD |
| TUI | Textual | MIT |
| Web UI | Svelte (served by the core, built offline) | MIT |
| DB | SQLite + SQLCipher, FTS5, sqlite-vec | Public domain / BSD-style / MIT-Apache |
| Tools protocol | MCP Python SDK | MIT |
| Sandbox | Podman (Docker fallback) | Apache-2.0 |
| Parsing | Docling, pypdfium2 | MIT, Apache-2.0/BSD |
| Redaction | Presidio + secret rules | MIT |
| Search | SearXNG (external) | AGPL-3.0 (separate process) |
| Drive sync | rclone or Google API client | MIT / Apache-2.0 |
| Zotero | pyzotero / local API | MIT |
| Secrets | keyring | MIT |

---

## 7. Delivery plan

### Phase 0 — Skeleton (thin slice of everything, D17)
Goal: every feature exists end to end in a minimal form.

| Feature | Thin-slice scope | Acceptance criterion |
|---|---|---|
| Core daemon + API | start/stop, auth token, health | `incant serve` + `incant status` work; API is only reachable locally |
| Backends | Ollama + OpenAI-compat drivers | Streams a reply from `qwen3:30b` via Ollama **and** via llama-server |
| TUI | chat list, chat view, input, sidebar | All §4.6 shortcuts work; `d` deletes with confirm + undo |
| Web UI | same screens, read/write | Same shortcuts work; works with the network disconnected |
| Folders | create, move, one level | `f` moves chat; folder survives restart |
| Effort | 3 levels mapped per §4.2 | Low uses the fast model; High enables thinking; recorded in audit |
| Environments | load TOML, picker, 2 templates | Switching env changes tools/network policy, and this is visible in audit |
| Context meter | tokens used / limit + compaction | Meter is within ±5% of the server's token count; compaction triggers at 80% |
| Uploads | text, PDF (Docling), image → vision | A PDF question gets an answer citing page numbers; image described by the vision model |
| Artifacts | write + versions + preview | Agent writes `notes.md`; 2nd edit makes v2; web preview loads in sandboxed iframe |
| Tools/MCP | fs, shell(sandbox), rag | Tool calls audited; shell has no network |
| Skills | loader + 1 skill (`lit-review` stub) | Skill shown in list; permissions it requests are enforced |
| Coding | diff approve + sandbox test run | Agent fixes a failing test in a sample repo with ≤2 approvals |
| Lit review | local PDFs + arXiv search | Produces a table + `.bib` from 5 PDFs; arXiv call appears in egress report |
| Zotero | read collections (local API) | Lists items from a chosen collection |
| Connectors | git clone/fetch; Drive folder mirror | Mirror a Drive folder read-only; agent reads local copy only |
| Web search | SearXNG backend, opt-in | Blocked in offline env; allowed + logged in online env |
| Multi-agent | `spawn_agent` (max_parallel 1) + 1 YAML workflow | Sub-agent appears nested in audit; budget limits enforced |
| Audit log | hash chain + verify + per-chat view | `incant audit verify` fails after a record is edited by hand |
| Redaction | secret rules + Presidio | A fake AWS key in an outbound query is blocked |
| Encryption | SQLCipher DB + encrypted uploads | DB file can't be read without the passphrase |
| Egress report | per-chat panel | Lists host, bytes and redactions for each request |

### Phase 1 — Depth: coding
Hunk-level approval, git commit flow, LSP-backed code search, test-and-fix skill, repo indexing.

### Phase 2 — Depth: literature review
Semantic Scholar/OpenAlex/PubMed/Crossref, dedupe, screening criteria, extraction schemas, automatic citation-check, Zotero write-back (opt-in).

### Phase 3 — Orchestration and hardening
Workflow editor, parallel sub-agents on GPU backends, signed audit checkpoints, Rust rewrite of audit log + egress proxy (D9), encrypted-artifacts mode.

---

## 8. Open questions
1. **Hardware specifics:** how much system RAM and VRAM do you have, and which OS? This decides which vision model is feasible, whether the main + fast + embedding models fit in memory together, and the realistic context limit.
2. **Artifact versioning:** a `.versions/` folder or a small git repo per chat? (Git gives diffs and history for free.)
3. **Artifact encryption:** is "plaintext artifacts + full-disk encryption" acceptable, or must artifacts also be encrypted by incant?
4. **Unlock UX:** passphrase on every daemon start, or an OS-keychain auto-unlock?
5. **Drive sync scope:** which folders, and should it be on-demand only or scheduled?
6. **Citation style** defaults for literature output (APA, IEEE, ...)?
7. **Name:** keep `incant`?

## 9. Risks
- **CPU speed:** long prompts and sub-agents are slow. Mitigated by retrieval, compaction, KV-cache reuse, the fast model for routing, and running sub-agents one at a time.
- **Tool-calling reliability** of 30B-class local models. Mitigated by JSON-schema-constrained output (grammar in llama.cpp), small tool sets per environment, and retry/repair.
- **Scope:** "thin slice of everything" covers many features. Each item is kept to its Phase 0 acceptance criterion; anything beyond it moves to later phases.
- **Prompt injection** from PDFs/web pages. Mitigated by tagging untrusted content, requiring approval for network and write actions, and the audit trail.
