# Cairnward requirements

Status: Draft v0.4 (goes with spec v0.8; v0.3 went with spec v0.7: all protections optional; v0.1 described the all-in-one design and is in git history)

All security requirements below describe what each protection does **when it is switched on**; FR-PRIV-* require that each can be switched off.

- **IDs** are stable: never reuse or renumber. v0.1 IDs that no longer apply are listed at the end as withdrawn.
- **Priority (MoSCoW) for Phase 0:** **M** Must, **S** Should (may slip to 0.x), **C** Could, **W** Won't in Phase 0.
- **MS:** milestone M0–M3 from [plan/phase0.md](plan/phase0.md); L = later.
- **Verification:** the test proving it. Every **M** item needs an automated test before Phase 0 closes.

---

## Functional requirements

### Privacy settings and presets
| ID | Requirement | Pri | MS | Verification |
|---|---|---|---|---|
| FR-PRIV-1 | `[privacy]` settings (spec §5.0 table) with presets Open / Balanced / Strict; individual overrides mark *Custom* | M | M0 | `test_privacy_settings.py` |
| FR-PRIV-2 | Resolution environment → global; virtual models carry the environment's privacy level; `cairnward run --env` applies it | M | M0 | `test_privacy_resolution.py` |
| FR-PRIV-3 | `cairnward setup` first-run preset choice + prerequisite check; unmet prerequisites shown as *requested but inactive* | M | M0 | integration |
| FR-PRIV-4 | `cairnward privacy show [--env]`, status badge in CLI and dashboard; warnings for ineffective combinations (e.g. allowlist without isolation = advisory) | M | M0 (CLI), M2 (dashboard) | unit + e2e |
| FR-PRIV-5 | Every settings change audited; `audit.disabled` written before audit is switched off, `audit.enabled` with gap duration when back on | M | M0 | `test_settings_audit.py` |
| FR-PRIV-6 | Each protection can be switched off independently and Cairnward keeps working; full test suite passes under Open, Balanced, Strict and all-off | M | M0 onward | CI matrix over presets |

### Gateway: model endpoint and backends
| ID | Requirement | Pri | MS | Verification |
|---|---|---|---|---|
| FR-GW-1 | OpenAI-compatible `/v1/chat/completions` (streaming, tool calls), `/v1/models`, `/v1/embeddings` | M | M0 | integration tests with fake backends; conformance against the OpenAI SDK |
| FR-GW-2 | Per-client API keys (`cairnward client add`); requests attributed to a client in audit/vault | M | M0 | `test_client_keys.py` |
| FR-GW-3 | Virtual models `cw/<environment>:<effort>` resolve environment + effort settings | M | M1 | `test_virtual_models.py` |
| FR-GW-4 | Per-client, per-run and per-environment budgets (tokens, requests, time) | M | M2 | `test_budgets.py` |
| FR-GW-5 | Context warning: request expected to need > 60 s prompt processing is flagged (dashboard + optional rejection) | S | M2 | unit with benchmark profile |
| FR-GW-6 | Ollama-compatible endpoint | W | L | — |
| FR-GW-7 | Gateway never modifies a conversation's prompt prefix (additions go at the end) | M | M0 | `test_prefix_stability.py` (byte-identical prefixes across turns) |
| FR-GW-8 | Conversation affinity, keep-alive while active, unload after idle | M | M1 | integration with fake backend tracking cache hits |
| FR-GW-9 | KV-cache save/restore per conversation with llama.cpp slots | S | M2 | integration with llama-server |
| FR-GW-10 | Memory check before loading a model (size + context vs available RAM); warn or queue | M | M1 | unit with mocked meminfo |
| FR-GW-11 | Waiting-time estimate from stored benchmark profile; `cairnward bench` imports/re-runs the profile | M | M1 | unit |
| FR-BACK-1 | Ollama and OpenAI-compatible backend drivers (streaming, tools, embeddings, health) | M | M0 | integration with fake servers; manual against real Ollama + llama-server |
| FR-BACK-2 | Backends in `backends.toml`; model per role; override order run → client → environment → global | M | M1 | `test_backend_resolution.py` |
| FR-BACK-3 | Fallback only to local backends, logged | M | M1 | `test_backend_fallback.py` |
| FR-BACK-4 | Hosted backends denied by default; never for locked conversations unless overridden; ☁ marking | M | M3 | `tests/security/test_hosted_policy.py` |
| FR-BACK-5 | Tested hosted providers: Alibaba Model Studio, DeepSeek API, OpenRouter | S | M3 | manual, release notes |
| FR-BACK-6 | Anthropic driver (Claude API key) | W | L | — |

### Isolation and egress (OS-enforced)
| ID | Requirement | Pri | MS | Verification |
|---|---|---|---|---|
| FR-ISO-1 | `cairnward run -- <cmd>` runs a command under bubblewrap with a loopback-only network namespace; gateway, MCP and proxy reachable via bridged sockets | M | M0 | `tests/isolation/test_run_sandbox.py` (direct TCP/UDP/DNS fail; gateway works) |
| FR-ISO-2 | Filesystem policy for runs: workspace rw, agent config rw, secrets dirs hidden, rest read-only | M | M0 | `tests/isolation/test_run_fs.py` |
| FR-ISO-3 | Cairnward core runs in its own loopback-only namespace; only `cairnward-egress` has network | M | M0 | `tests/isolation/test_core_isolated.py` |
| FR-ISO-4 | Container templates for server clients on an internal-only network | M | M2 | integration (Podman) |
| FR-ISO-5 | `cairnward doctor` checks prerequisites and runs isolation self-tests | M | M0 | unit + isolation suite |
| FR-NET-1 | Network off by default; host allowlist / ask per host; every connection logged (host, bytes, timing, client/run) | M | M0 | `tests/security/test_no_egress_default.py` |
| FR-NET-2 | Optional TLS interception with local CA, off by default, per environment | C | L | — |
| FR-EGR-1 | Egress report per conversation, run and client; export | M | M2 | e2e |

### Policy and approvals
| ID | Requirement | Pri | MS | Verification |
|---|---|---|---|---|
| FR-POL-1 | `allow/ask/deny` decided outside the model for every effect | M | M0 | unit + coverage test |
| FR-POL-2 | Risk tiers (low → allow+log, medium → ask once per scope, high → always ask) | M | M2 | `test_risk_tiers.py` |
| FR-POL-3 | Approvals inbox (dashboard, desktop notification, `cairnward approvals`); batching of similar requests | M | M2 | e2e |
| FR-POL-4 | User-only actions open a confirmation dialog in Cairnward's UI showing action + provenance; passphrases entered only there | M | M2 | `tests/security/test_user_only_actions.py` + injection corpus |
| FR-POL-5 | Approvals survive client retries after timeouts | S | M2 | integration |

### MCP proxy and tools
| ID | Requirement | Pri | MS | Verification |
|---|---|---|---|---|
| FR-MCP-1 | MCP proxy (streamable HTTP + stdio shim) aggregating enabled servers per environment | M | M2 | integration |
| FR-MCP-2 | Permissions = manifest ∩ environment; third-party servers sandboxed | M | M2 | `test_tool_permissions.py` |
| FR-MCP-3 | Version/hash pinning; tool-definition hashes; change → disabled until re-approved | M | M2 | `tests/security/test_mcp_rugpull.py` |
| FR-MCP-4 | Skills (SKILL.md) served to clients and used by research workflows | S | M2 | integration |

### Coding agents
| ID | Requirement | Pri | MS | Verification |
|---|---|---|---|---|
| FR-AGT-1 | Profiles for Aider (first, M0), Qwen Code, opencode, Goose + a generic OpenAI-compatible profile; each profile records its typical prompt size and is flagged when it exceeds the virtual model's context or the 60 s threshold | M | M2 (Aider in M0) | compatibility tests with fake model server |
| FR-AGT-2 | Workspace snapshots before/after each run on hidden refs; `runs diff`, `runs revert` | M | M1 | `test_run_snapshots.py` |
| FR-AGT-3 | All model/tool calls of a run linked to the run in audit and vault | M | M0 | integration |

### Chat clients
| ID | Requirement | Pri | MS | Verification |
|---|---|---|---|---|
| FR-CLI-1 | Tested setup guides + profiles for Open WebUI and Jan | M | M2 | compatibility tests; manual checklist |
| FR-CLI-2 | Third tested client (LibreChat or AnythingLLM) | S | M2 | same |

### Transcript vault
| ID | Requirement | Pri | MS | Verification |
|---|---|---|---|---|
| FR-VLT-1 | Store every request/response through the model endpoint, per-conversation age keys | M | M0 | integration |
| FR-VLT-2 | Reconstruct conversations from stateless requests by prefix hashing; branches for divergence | M | M1 | Hypothesis property test |
| FR-VLT-3 | Vault browser: search, folders, pin, export, delete with confirm/undo; shortcuts (`d`, `f`, `/`, `j/k`, `L`, `x`, `l`, `?`); single-key shortcuts can be turned off | M | M2 | Playwright e2e |
| FR-VLT-4 | Per-environment retention and `vault = "off"` (digests only) | M | M2 | unit |
| FR-ENC-1 | Lock conversation / artifacts with passphrase (age, key wrapping, recovery key optional) | M | M2 | `test_lock.py` |
| FR-ENC-2 | Passphrase never in model context, tool args, logs or audit | M | M2 | `tests/security/test_passphrase_leak.py` |
| FR-ENC-5 | Crypto-shredding on purge | M | M1 | `test_crypto_shred.py` |

### Research workflows
| ID | Requirement | Pri | MS | Verification |
|---|---|---|---|---|
| FR-RES-1 | PDF library from a local folder (pypdfium2 default; Docling optional worker), chunks, embeddings | M | M1 | integration |
| FR-RES-2 | `lit.ask`: answers with page citations | M | M1 | eval `evals/pdf_qa` |
| FR-RES-3 | Research jobs: queue, progress, resumable after restart, schedulable | M | M1 | `test_jobs_resume.py` |
| FR-RES-4 | Review pipeline: screen (fast model, abstracts) → extract (main model, full text) → synthesise → comparison table + `.bib`; work grouped by model; estimated finish time | M | M3 | eval `evals/litreview_table` |
| FR-RES-5 | Citation check: every reference resolves; every quotation matches PDF text | M | M1 | eval `evals/citation_existence` (zero invented) |
| FR-RES-6 | CSL formatting; APA and IEEE shipped; `.csl` adds styles | M | M1 | golden tests |
| FR-RES-7 | Zotero via local API | S | M3 | fake Zotero API |
| FR-RES-8 | arXiv search (opt-in, cached, audited) | M | M3 | recorded responses |
| FR-RES-9 | Semantic Scholar, OpenAlex, PubMed, Crossref | S | M3 | recorded responses |
| FR-RES-10 | Research tools exposed via MCP (`lit.search`, `lit.ask`, `lit.review.start`) | M | M2 | integration |
| FR-RES-11 | Sub-agent fan-out in workflows with budgets, `max_parallel`, nested audit; YAML workflows | S | M3 | `test_workflow_budgets.py` |
| FR-RES-12 | PaperQA2 spike with written recommendation | M | M1 | ADR |

### Artifacts
| ID | Requirement | Pri | MS | Verification |
|---|---|---|---|---|
| FR-ART-1 | Git repo per conversation/job/run; one commit per write; restore = new commit | M | M1 | `test_artifacts_git.py` |
| FR-ART-4 | HTML/SVG previews on a sandboxed origin without network | M | M2 | Playwright |

### Audit
| ID | Requirement | Pri | MS | Verification |
|---|---|---|---|---|
| FR-AUD-1 | Pre- and post-records for every effect with actor (client/agent/run/user), decision, digests | M | M0 | `test_audit_coverage.py` |
| FR-AUD-2 | Hash chain; `audit verify` detects edits/removals | M | M0 | Hypothesis tamper tests |
| FR-AUD-3 | Audit browser (per client/run/conversation), export | M | M2 | e2e |
| FR-AUD-4 | Signed checkpoints anchored hourly + on demand to a private git repo (deploy key scoped to it); `audit verify --anchors` | M | M2 | `test_anchoring.py` with local bare repo |
| FR-AUD-5 | OpenTelemetry GenAI export (local), off by default | C | M3 | integration |

### Redaction
| ID | Requirement | Pri | MS | Verification |
|---|---|---|---|---|
| FR-RED-1 | Built-in scanner (secrets, emails, phones, ID numbers, custom regex) on traffic Cairnward terminates; secrets block, PII ask | M | M2 | `tests/security/test_redaction.py` |
| FR-RED-2 | Presidio as optional worker | S | M3 | integration |

### Sync and backup
| ID | Requirement | Pri | MS | Verification |
|---|---|---|---|---|
| FR-SYNC-1 | Modes none/on_demand/auto for every job | M | M3 | `test_sync_modes.py` |
| FR-SYNC-3 | restic backups of vault, artifacts, research library; retention; restore to date | M | M3 | local restic repo |
| FR-SYNC-4 | Replication via per-device event logs + git; deterministic merge; fork on concurrent turns | S | M3 | Hypothesis convergence |
| FR-SYNC-6 | Locked items only written encrypted; cloud warning; "encrypt this sync target" | M | M3 | `tests/security/test_sync_encryption.py` |
| FR-SYNC-8 | Purge rewrites restic snapshots and prunes; pending state shown; retries | M | M3 | `test_backup_purge.py` (content absent from all snapshots) |
| FR-SYNC-2 | Drive mirror (read-only) via rclone with preview | S | M3 | rclone local backend |

### Dashboard
| ID | Requirement | Pri | MS | Verification |
|---|---|---|---|---|
| FR-DASH-1 | Loopback dashboard with Host/Origin/CSRF protections, login link | M | M2 | `tests/security/test_local_api.py` |
| FR-DASH-2 | Views: approvals, activity, audit, egress, vault, jobs, artifacts, sync, settings | M | M2 (activity/approvals M0 as CLI) | e2e |
| FR-DASH-3 | WCAG 2.2 AA | S | M2 | axe-core + manual |

### Supply chain
| ID | Requirement | Pri | MS | Verification |
|---|---|---|---|---|
| FR-SUP-2 | Model downloads via egress proxy: GGUF/safetensors only, SHA-256 verified, audited | M | M2 | unit |

---

## Non-functional requirements

| ID | Requirement | Pri | Verification |
|---|---|---|---|
| NFR-P2 | Gateway adds ≤ 50 ms per request excluding model time (Strict); ≤ 10 ms with all protections off | M | benchmark test with fake backend |
| NFR-P3 | Follow-up turns reuse the prompt cache: ≥ 10× faster first token than cold (reference machine measured 18.6×) | M | benchmark `--cache-test` through the gateway |
| NFR-P6 | 512-token prompt: first token ≤ 15 s on the fast model, ≤ 30 s on the main model (reference machine) | S | benchmark through the gateway |
| NFR-P5 | Core ≤ 500 MB RAM without optional workers; starts ≤ 3 s | S | CI measurement |
| NFR-R1 | No data loss on crash; research jobs resumable | M | kill tests |
| NFR-R2 | Audit record before every effect | M | FR-AUD-1 |
| NFR-S1 | Strict: zero outbound connections unless allowlisted; Balanced: none without approval | M | isolation suite per preset |
| NFR-S2 | Threat model reviewed each milestone | M | PR checklist |
| NFR-S4 | With isolation on, isolation holds for every sandbox type (direct network attempts fail) | M | isolation suite in CI (Linux) |
| NFR-A1 | Dashboard WCAG 2.2 AA | S | axe-core |
| NFR-PORT-1 | Linux (Debian 12+, Ubuntu 22.04+); Python 3.12+; unit tests also pass on Windows | M | CI |
| NFR-PORT-2 | Windows via WSL2 | W | L |
| NFR-SUP-1 | Hash-locked deps; pip-audit; license check; pinned CI actions | M | CI |
| NFR-SUP-2 | SBOM + Sigstore-signed releases | S | release workflow |
| NFR-MAINT-1 | ruff + mypy strict; ≥ 80% coverage on core packages | M | CI |

---

## Withdrawn (from v0.1)
FR-API-1–5 → FR-GW-*, FR-DASH-1; FR-CHAT-1–3, FR-UI-1–4 (own chat clients) → FR-VLT-3, FR-CLI-*; FR-EFF-*, FR-ENV-* → FR-GW-3 (definitions unchanged); FR-CTX-1–2 → FR-GW-5 and client `usage` reporting; FR-UP-* (uploads via client) → client feature; FR-CODE-1–4 (own coding flow) → FR-AGT-*; FR-TOOL-1–4 → FR-MCP-*; FR-LIT-*, FR-CITE-* → FR-RES-*; FR-AGENT-* → FR-RES-11; FR-ENC-3 → FR-POL-4; FR-SYNC-5, FR-SYNC-7 → covered by FR-SYNC-4 and FR-POL-4; FR-SUP-1 → FR-ISO-5; FR-SUP-3 → FR-SUP-2; NFR-P1, NFR-P4, NFR-S3, NFR-OFF-1 → NFR-P2, FR-GW-5, FR-DASH-1, NFR-S1.
