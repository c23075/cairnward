# Phase 0 plan: milestones and tasks

Status: Draft v0.3 (goes with spec v0.7; earlier plans are in git history)
Approach: **walking skeleton first** (M0), then milestones that each add complete features. Requirement IDs refer to [requirements.md](../requirements.md). Sizes: S ≈ ≤ 1 day, M ≈ 2–4 days, L ≈ 1–2 weeks of focused work.

---

## M0: Gateway skeleton
**Goal:** a coding agent and a chat client talk to Ollama **through Cairnward**, inside sandboxes with no direct network; every call is policy-checked, audited and stored in the vault.

| # | Task | Reqs | Depends | Size |
|---|---|---|---|---|
| T0.1 | Project setup: uv, ruff, mypy strict, pytest, CI (Debian 12, Ubuntu, Windows unit tests), pip-audit, Dependabot | NFR-MAINT-1, NFR-SUP-1 | — | S ✅ |
| T0.2 | Benchmark script; run on reference machine; record results in `docs/bench/` | NFR-P3 | — | S ✅ (results 2026-11-01; optional flash-attention test pending) |
| T0.3 | Rename package to `cairnward`; Apache-2.0 LICENSE | D51 | — | S ✅ |
| T0.4 | Config: pydantic models for `cairnward.toml`, `backends.toml`, `clients.toml`; JSON Schema export | spec §11 | T0.3 | S ✅ |
| T0.4b | Privacy settings model: `[privacy]` table, presets, environment overrides, `privacy show`, settings-change audit hooks; CI matrix over presets | FR-PRIV-1, FR-PRIV-2, FR-PRIV-4, FR-PRIV-6 | T0.4 | M (✅ settings, presets, overrides, `privacy show`, settings-change audit records; CI preset matrix once features exist) |
| T0.5 | Store: SQLite (WAL) + Alembic baseline; vault tables; per-conversation age keys | FR-VLT-1, FR-ENC-5 (partial) | T0.4 | M ✅ (vault.db + keys.db; trash/purge with crypto-shredding; `store info`) |
| T0.6 | **Sandbox spike:** bubblewrap + loopback-only netns + Unix-socket bridges; decide reuse of `sandbox-runtime` vs own implementation (ADR-0013) | FR-ISO-1 | T0.3 | M ✅ reuse srt; 13/13 checks + chaining pass; CI job `isolation-spike` |
| T0.7 | Audit log: hash chain, `AuditSink`, `cairnward audit verify` | FR-AUD-1, FR-AUD-2 | T0.5 | M ✅ (audit.db + audit.jsonl mirror; pre/post `effect()` API; `audit verify/head/log`; settings-change records incl. `audit.disabled`/`enabled`) |
| T0.8 | Policy engine skeleton (`allow/ask/deny`, default rules, CLI approval prompt) | FR-POL-1 | T0.7 | M ✅ (rules for model/network/files/exec/user-only actions; risk tiers; remembered approvals; terminal approver; `guard()` with audit; `policy explain`) |
| T0.9 | `cairnward-egress`: HTTP(S) CONNECT proxy, host allowlist, connection log; network off by default | FR-NET-1 | T0.8 | M ✅ (policy per connection, session tokens, DNS-rebinding guard, audit with byte counts, `cairnward egress`; srt → egress chaining with token attribution verified manually; automated in T0.12) |
| T0.10 | Backend drivers (`ollama`, `openai_compat`) with fake servers for tests | FR-BACK-1 | T0.4 | M ✅ (Ollama native API with num_ctx/think/keep_alive, tool calls and images; OpenAI-compatible pass-through with `enable_thinking`; streaming; remote backends via egress) |
| T0.11 | Gateway endpoint `/v1/chat/completions` (+ streaming, tools), `/v1/models`; per-client API keys; vault write; prompt prefixes never modified | FR-GW-1, FR-GW-2, FR-GW-7, FR-VLT-1 | T0.8, T0.10 | M ✅ (virtual models, per-client keys, policy + audit per call, messages forwarded unchanged, vault recording with conversation continuation and branching; `serve`, `client`, `api schema`; served end to end) |
| T0.12 | `cairnward run` (sandbox from T0.6, env/config injection) + first agent profile (**Aider**: leanest prompts, best fit for CPU) | FR-ISO-1, FR-ISO-2, FR-AGT-1 (first), FR-AGT-3 | T0.6, T0.11 | M ✅ (`cairnward run --profile aider|qwen-code|generic -- <cmd>`: per-run key and egress session via the management socket, clean environment, srt sandbox, run.start/run.end audited; verified end to end with a stand-in agent; real Aider check pending on the reference machine) |
| T0.13 | Run the core itself in an isolated namespace; only `cairnward-egress` networked | FR-ISO-3 | T0.9, T0.12 | M ✅ (isolated mode: core in srt serving Unix sockets; egress process forwards 127.0.0.1:<gateway_port> and is the only networked process; SIGTERM handling + core watchdog; e2e test in CI) |
| T0.14 | `cairnward doctor` with isolation self-tests; `cairnward setup` first-run preset choice | FR-ISO-5, FR-PRIV-3, FR-PRIV-5 | T0.12, T0.4b | M ✅ (`doctor`: prerequisites, model presence, memory estimate, isolation self-test in the real sandbox, audit check, disk encryption hint; `setup`: preset choice, optional sandbox install; `status`; `sandbox install|status`) |
| T0.15 | Open WebUI (or Jan) connected via internal network or loopback for a first manual check | FR-CLI-1 (partial) | T0.11 | S ◐ (guides for Open WebUI and Jan; official OpenAI SDK compatibility tests; hands-on Open WebUI check pending (owner)) |

**Exit criteria (M0)**
- An agent run with `cairnward run` edits a sample repo using `qwen3:30b` via Ollama; its direct network attempts fail; all its model calls are in the audit log and vault.
- A chat client conversation shows up in the vault.
- `cairnward audit verify` passes; hand-edited records are detected.
- Strict: zero outbound connections (isolation suite green in CI on Linux); the same agent run also works with everything switched off.
- `cairnward setup` → choose preset → `cairnward privacy show` reflects it; changing a setting is audited and the badge updates.

---

## M1: Research core
**Goal:** ask questions over a PDF library with page citations; run a basic review job overnight; citations verified and formatted.

| # | Task | Reqs | Depends | Size |
|---|---|---|---|---|
| T1.1 | **PaperQA2 spike** via Cairnward's endpoint on CPU: quality, speed, dependencies; ADR with decision | FR-RES-12 | M0 | M ✅ (ADR-0014: own lean pipeline by default, PaperQA2 optional deep engine later; `/v1/embeddings` added) |
| T1.2 | PDF library (pypdfium2), chunking, embeddings (sqlite-vec) | FR-RES-1 | T1.1 | M ✅ (research.db; ~350-token page-aware chunks; hybrid vector + keyword search; resumable indexing; scanned/broken PDFs flagged; `cairnward library` commands) |
| T1.3 | `lit.ask` with page citations | FR-RES-2 | T1.2 | M ✅ (CLI, management API, MCP tool; citation mapping and checks; `evals/pdf_qa` to run with real models) |
| T1.4 | Job queue: persistent, resumable, schedulable; progress in CLI | FR-RES-3 | M0 | M |
| T1.5 | Citation and quotation check | FR-RES-5 | T1.2 | M ✅ (answers and drafts; references by title, DOIs form-checked offline) |
| T1.6 | CSL formatting (APA, IEEE) | FR-RES-6 | — | S |
| T1.7 | Virtual models (environment × effort), `effort.toml`, environments | FR-GW-3, FR-BACK-2, FR-BACK-3 | M0 | M |
| T1.8 | Vault conversation reconstruction (prefix hashing, branches) | FR-VLT-2 | M0 | M |
| T1.9 | Artifacts git repos; run workspace snapshots, `runs diff/revert` | FR-ART-1, FR-AGT-2 | M0 | M ◐ (run snapshots ✅: shadow git store per workspace, `runs list/diff/revert/prune`, conflicts protected, project `.git` untouched; per-conversation artifact repos still to do) |
| T1.10 | Crypto-shredding purge | FR-ENC-5 | T1.8 | S |
| T1.11 | Cache-aware scheduling (affinity, keep-alive, idle unload), memory check, waiting-time estimate from benchmark profile | FR-GW-8, FR-GW-10, FR-GW-11 | M0 | M ◐ (✅ per-backend queue with conversation affinity, cached-prefix estimate, waiting-time warnings from `cairnward bench import`, keep-alive; ✅ memory check before loading a model (FR-GW-10, done in M2 prep); idle unload left to the model server's keep_alive) |

**Exit criteria (M1)**: `lit.ask` eval passes with page citations; citation eval shows zero invented references; a review job survives a restart mid-run; picking `cw/coding:low` vs `cw/coding:high` in a client changes model and thinking.

---

## M2: Security features, dashboard, clients and agents
| # | Task | Reqs | Size |
|---|---|---|---|
| T2.1 | Dashboard shell with Host/Origin/CSRF protections, login link | FR-DASH-1 | M |
| T2.2 | Approvals inbox, risk tiers, batching, desktop notifications, retry-aware approvals | FR-POL-2, FR-POL-3, FR-POL-5 | L |
| T2.3 | User-only action dialog with provenance; injection corpus tests | FR-POL-4 | M |
| T2.4 | Redaction (built-in scanner) on terminated traffic | FR-RED-1 | M |
| T2.5 | Egress report, audit browser, activity view | FR-EGR-1, FR-AUD-3, FR-DASH-2 | L |
| T2.6 | Vault browser with shortcuts; retention; `vault = "off"` | FR-VLT-3, FR-VLT-4 | L |
| T2.7 | Locking (age wrapping, recovery key), passphrase-leak tests | FR-ENC-1, FR-ENC-2 | M |
| T2.8 | Audit anchoring to private git repo | FR-AUD-4 | M |
| T2.9 | MCP proxy, permissions, pinning, sandboxed servers; research tools via MCP | FR-MCP-1–3, FR-RES-10 | L |
| T2.10 | All four agent profiles + compatibility tests | FR-AGT-1 | M ◐ (✅ Aider, Qwen Code, opencode tested with the real agents in the sandbox under Strict; Goose profile untested; eval harness `evals/coding/`) |
| T2.11 | Client guides + container templates: Open WebUI, Jan, third client | FR-CLI-1, FR-CLI-2, FR-ISO-4 | M |
| T2.12 | Budgets and context warning; llama.cpp KV save/restore | FR-GW-4, FR-GW-5, FR-GW-9 | M ◐ (✅ run budgets (requests/tokens/minutes) and context-overflow warn/reject; ✗ llama.cpp KV save/restore (FR-GW-9)) |
| T2.13 | Model download verification via egress | FR-SUP-2 | S |
| T2.14 | Artifact previews (sandboxed origin) | FR-ART-4 | S |

**Exit criteria (M2)**: security suites green (injection corpus, isolation, local API, passphrase leaks, MCP rug-pull); four agents and two clients pass compatibility tests; anchors detect tampering older than the last anchor.

---

## M3: Sync, sources, workflows, hosted backends
| # | Task | Reqs | Size |
|---|---|---|---|
| T3.1 | Sync framework and modes | FR-SYNC-1 | M |
| T3.2 | restic backups + **purge rewrite** | FR-SYNC-3, FR-SYNC-8 | M |
| T3.3 | Replication (event logs + git), encrypted locked items | FR-SYNC-4, FR-SYNC-6 | L |
| T3.4 | Drive mirror | FR-SYNC-2 | M |
| T3.5 | Zotero, arXiv, other academic APIs | FR-RES-7–9 | M |
| T3.6 | Review pipeline + YAML workflows with sub-agent fan-out | FR-RES-4, FR-RES-11 | L |
| T3.7 | Hosted backends policy; provider tests | FR-BACK-4, FR-BACK-5 | M |
| T3.8 | Presidio worker; OpenTelemetry export | FR-RED-2, FR-AUD-5 | S |
| T3.9 | Optional PaperQA2 deep-engine worker, compared on the lean pipeline's evals | FR-RES-13 | M |

**Exit criteria (M3 = Phase 0 done)**: every Must requirement has a passing test; a review of 5+ PDFs produces table + `.bib` with zero invented citations; a purged conversation is absent from every backup snapshot; threat model reviewed.

## Later
Windows via WSL2 (NFR-PORT-2), Ollama-compatible endpoint, Rust `cairnward-egress`, Merkle checkpoints, TLS interception option.

## Immediate next steps
1. Owner: run the benchmark; rename the repo and fix the default branch.
2. T0.4 → T0.6 (config, store, sandbox spike), then the rest of M0 in order.
