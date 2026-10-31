# Phase 0 plan: milestones and tasks

Status: Draft v0.1 (goes with spec v0.5)
Approach: **walking skeleton first** (M0: the thinnest path through every layer, deployed and tested), then milestones that each add complete features end to end. Requirement IDs refer to [requirements.md](../requirements.md).

Rules
- A milestone is done when its **exit criteria** pass in CI on Debian and Windows (model evals: on the reference machine).
- Tasks are ordered; a task may start when the tasks it depends on are done.
- Each task is one or a few PRs, each with tests. The size column is a rough guide: S ≈ ≤ 1 day, M ≈ 2–4 days, L ≈ 1–2 weeks of focused work.

---

## M0: Walking skeleton
**Goal:** type a message in the TUI, get a streamed reply from `qwen3:30b` via Ollama, with the call passing through the policy engine and recorded in a verifiable audit log, with the network blocked by default.

| # | Task | Reqs | Depends | Size |
|---|---|---|---|---|
| T0.1 | Project setup: uv, package layout, ruff, mypy strict, pytest, CI matrix (Debian 12, Ubuntu 24.04, Windows), pip-audit, Dependabot | NFR-MAINT-1, NFR-PORT-1, NFR-SUP-1 | — | S ✅ (Scorecard + license check: add with first runtime deps) |
| T0.2 | Benchmark script for Ollama/OpenAI-compatible servers; run on reference machine; record results in `docs/bench/` | NFR-P3, NFR-P4 | — | S ✅ (script); run pending |
| T0.3 | Config loading: pydantic models for `backends.toml`, `cairn.toml`; JSON Schema export; `schema_version` | spec §10 | T0.1 | S |
| T0.4 | Store: SQLite (WAL) + Alembic baseline migration; chats/messages tables; per-chat age keys (unlocked mode) | FR-ENC-5 (partial), NFR-R1 | T0.3 | M |
| T0.5 | Audit log: record model, hash chain, `AuditSink` interface, `cairn audit verify` | FR-AUD-1, FR-AUD-2 | T0.4 | M |
| T0.6 | Policy engine skeleton: `Action` types, `allow/ask/deny`, default rules, wired before every effect | FR-POL-1 | T0.5 | M |
| T0.7 | Egress proxy skeleton: all outbound HTTP via one client; network off by default; loopback model server exempt by config | FR-NET-1, NFR-S1 | T0.6 | S |
| T0.8 | Backend drivers: `ollama` + `openai_compat` (streaming chat, health); fake servers for tests | FR-BACK-1 | T0.3 | M |
| T0.9 | Core daemon: FastAPI over Unix socket (0600), token file, `/v1/health`, `/v1/chats`, streaming over WebSocket; OpenAPI committed + CI diff check | FR-API-1, FR-API-2, FR-API-4 | T0.6, T0.8 | M |
| T0.10 | Minimal TUI: chat list + chat view + input, streaming | FR-UI-1 (partial) | T0.9 | M |
| T0.11 | `cairn doctor`: checks Ollama, git, Podman/bwrap, restic, rclone; explains fixes | FR-SUP-1 | T0.1 | S |
| T0.12 | Minimal web UI shell served on loopback (login link, chat list, chat view) to prove the second client path | FR-UI-2 (partial) | T0.9 | M |

**Exit criteria (M0)**
- `cairn serve` + TUI: message → streamed reply from real Ollama on the reference machine.
- The same flow in the web UI shell.
- `cairn audit verify` passes; editing any audit row by hand makes it fail.
- With the network blocked, the full test suite passes and the egress log is empty.
- CI green on Debian 12, Ubuntu 24.04 and Windows.

---

## M1: Core experience
**Goal:** daily-usable chat app with the Claude-app basics.

| # | Task | Reqs | Depends | Size |
|---|---|---|---|---|
| T1.1 | Chats: rename, pin, archive, FTS search, auto-title via fast model | FR-CHAT-1 | M0 | M |
| T1.2 | Folders (one level), move | FR-CHAT-2 | T1.1 | S |
| T1.3 | Delete with confirm/undo, trash, purge with crypto-shredding | FR-CHAT-3, FR-ENC-5 | T1.1 | M |
| T1.4 | Shortcut system shared by both clients: keymap file, focus rules, `single_key` option, help overlay | FR-UI-1, FR-UI-2, FR-UI-3, FR-UI-4 | T1.1 | M |
| T1.5 | Web UI parity for chats, folders, shortcuts; Host/Origin/CSRF hardening | FR-UI-2, FR-API-3, NFR-S3 | T1.4 | L |
| T1.6 | Backends config: roles, override order, local-only fallback | FR-BACK-2, FR-BACK-3 | M0 | M |
| T1.7 | Effort levels: defaults + `effort.toml` custom levels; picker + `E` cycle | FR-EFF-1, FR-EFF-2 | T1.6 | M |
| T1.8 | Environments: TOML, templates, picker, audited switch; approval scopes | FR-ENV-1, FR-ENV-2, FR-POL-2 | T1.6 | M |
| T1.9 | Context meter with category breakdown; tokenizer from backend or local estimate | FR-CTX-1 | T1.6 | M |
| T1.10 | Uploads (text/code/PDF/DOCX/CSV/images) into the store; hashes audited | FR-UP-1 | M0 | M |
| T1.11 | Artifacts: git repo per chat, one commit per write, panel `A` | FR-ART-1 | M0 | M |

**Exit criteria (M1)**: all M1 "M" requirements pass their tests on all platforms; a week of the developer's own daily use without data loss.

---

## M2: Tools, coding and security features
**Goal:** the agent can use tools, edit code safely, and every security feature is in place.

| # | Task | Reqs | Depends | Size |
|---|---|---|---|---|
| T2.1 | MCP host; built-in `fs`, `rag`; manifests; permission intersection | FR-TOOL-1, FR-TOOL-2 | M1 | L |
| T2.2 | MCP pinning + tool-definition hashing + re-approval flow | FR-TOOL-4 | T2.1 | M |
| T2.3 | Sandbox runner: tier 1 (Podman), tier 2b (bwrap), tier 2 (WSL2+bwrap), tier 4 policy; `shell` MCP server | FR-CODE-2, FR-CODE-3 | T2.1 | L |
| T2.4 | Coding flow: diff proposal, per-file approval, apply via FS gateway; sample-repo eval | FR-CODE-1, FR-CODE-4 | T2.3 | L |
| T2.5 | Skills loader (SKILL.md); permissions enforced; `test-and-fix` stub | FR-TOOL-3 | T2.1 | M |
| T2.6 | PDF parsing (Docling, pypdfium2 fallback), chunking, embeddings, sqlite-vec index; PDF Q&A with page citations | FR-LIT-1, FR-UP-2 | T2.1 | L |
| T2.7 | Vision: on-demand vision model with unload/reload of fast model | FR-UP-3 | T1.10 | M |
| T2.8 | Auto-compaction | FR-CTX-2 | T1.9 | M |
| T2.9 | Artifact history/diff/restore; sandboxed preview origin; external edits | FR-ART-2, FR-ART-3, FR-ART-4 | T1.11 | M |
| T2.10 | Network allowlist/ask; redaction (secret rules + Presidio); egress report panel | FR-NET-2, FR-RED-1, FR-EGR-1 | M1 | L |
| T2.11 | Lock chat / lock artifacts (age, wrapping, recovery key); passphrase dialog in both clients; `user_initiated_only` actions | FR-ENC-1–4, FR-POL-3 | M1 | L |
| T2.12 | Audit UI (per chat, global, export); signed checkpoints | FR-AUD-3, FR-AUD-4 | M1 | M |
| T2.13 | Model download verification; reject pickle formats | FR-SUP-2, FR-SUP-3 | M1 | S |
| T2.14 | Security suites: injection corpus, rebinding/CSRF, passphrase-leak scan, sandbox isolation; schemathesis | NFR-S2, FR-API-5 | T2.10, T2.11 | M |

**Exit criteria (M2)**: security test suites green; injection corpus triggers no network, writes or user-only actions; coding eval passes on the reference machine.

---

## M3: Research, connectors, sync and orchestration
**Goal:** literature-review workflow, connectors, sync, multi-agent.

| # | Task | Reqs | Depends | Size |
|---|---|---|---|---|
| T3.1 | `cite.format` (citeproc-py, CSL: APA, IEEE) + `references` skill | FR-CITE-1 | M2 | M |
| T3.2 | `citation-check` + evals for invented references | FR-CITE-2 | T3.1 | M |
| T3.3 | arXiv tool (opt-in, cached, audited); comparison table + `.bib` | FR-LIT-2, FR-LIT-4 | T3.1 | M |
| T3.4 | Zotero (local API) read | FR-LIT-3 | M2 | S |
| T3.5 | SearXNG web search + `web.fetch` with untrusted-content tagging | FR-NET-3 | M2 | M |
| T3.6 | Sync framework: jobs, modes (none/on_demand/auto with debounce + cron), audit, `S`/`Shift+S` | FR-SYNC-1, FR-SYNC-7 | M2 | M |
| T3.7 | `backup` jobs via restic (local + rclone targets), restore, retention | FR-SYNC-3 | T3.6 | M |
| T3.8 | `replicate` jobs: event log format (`docs/formats/events.md`), per-device logs, merge, fork on concurrent turns, git artifacts; Hypothesis convergence tests | FR-SYNC-4, FR-SYNC-5 | T3.6 | L |
| T3.9 | Encrypted sync of locked items; "encrypt this sync target"; cloud warning | FR-SYNC-6 | T3.8, T2.11 | M |
| T3.10 | Drive folder mirror via rclone with preview | FR-SYNC-2 | T3.6 | M |
| T3.11 | `spawn_agent`, budgets, nested audit; `lit-review` YAML workflow | FR-AGENT-1–3 | M2 | L |
| T3.12 | Hosted backends policy, ☁ badge; test Alibaba Model Studio, DeepSeek, OpenRouter | FR-BACK-4, FR-BACK-5 | M2 | M |
| T3.13 | Optional OpenTelemetry export | FR-AUD-5 | M2 | S |
| T3.14 | Windows parity pass: tier 2 sandbox, keychain, paths, installer notes | NFR-PORT-1 | all | M |

**Exit criteria (M3 = Phase 0 done)**: every **Must** requirement has a passing automated test; lit-review eval produces a table + `.bib` from 5 PDFs with zero invented citations; two installs (Debian + Windows) replicate chats through a shared folder; threat model reviewed.

---

## Immediate next steps
1. Run `scripts/bench_backend.py` on the reference machine and commit the JSON result to `docs/bench/` (sets NFR-P3/P4).
2. T0.3 → T0.9 in order (config, store, audit, policy, egress, drivers, daemon).
