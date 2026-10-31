# Cairn requirements

Status: Draft v0.1 (goes with spec v0.5)

- **IDs** are stable: never reuse or renumber; mark withdrawn requirements as such.
- **Priority (MoSCoW) for Phase 0:** **M** = Must (Phase 0 isn't done without it), **S** = Should (in Phase 0, may slip to a 0.x release), **C** = Could (Phase 0 if time allows), **W** = Won't in Phase 0 (later phase).
- **Milestone:** M0–M3 from [plan/phase0.md](plan/phase0.md); P1–P3 = later phases.
- **Verification:** the test (or test suite) that proves it. Every **M** item must have an automated test before Phase 0 closes.

---

## Functional requirements

### Core service and API
| ID | Requirement | Pri | MS | Verification |
|---|---|---|---|---|
| FR-API-1 | `cairn serve` starts the core; `cairn status` reports health, backends and sandbox tier | M | M0 | `tests/integration/test_serve.py` |
| FR-API-2 | API listens on a per-user Unix socket (0600) by default; token file 0600 / per-user ACL | M | M0 | `tests/security/test_socket_perms.py` |
| FR-API-3 | Web UI port binds loopback only; rejects foreign Host and Origin; CSRF token on non-GET; no CORS | M | M1 | `tests/security/test_local_api.py` (DNS-rebinding, CSRF cases) |
| FR-API-4 | OpenAPI document committed; CI fails on undocumented changes | M | M0 | CI `openapi-diff` job |
| FR-API-5 | API fuzzed from the OpenAPI document without 5xx errors | S | M2 | schemathesis job |

### Model backends
| ID | Requirement | Pri | MS | Verification |
|---|---|---|---|---|
| FR-BACK-1 | Ollama and OpenAI-compatible drivers: streaming chat, tool calls, embeddings, health | M | M0 | integration tests with fake servers; manual check against real Ollama + llama-server |
| FR-BACK-2 | Backends defined in `backends.toml`; model per role (main/fast/vision/embed) with override order sub-agent → chat → environment → global | M | M1 | `test_backend_resolution.py` |
| FR-BACK-3 | Fallback only to other **local** backends, logged | M | M1 | `test_backend_fallback.py` |
| FR-BACK-4 | Hosted backends denied by default; allowed only via environment policy; never used for locked chats unless overridden per chat; ☁ badge | M | M3 | `tests/security/test_hosted_policy.py` |
| FR-BACK-5 | Tested providers: Alibaba Model Studio, DeepSeek API, OpenRouter | S | M3 | manual, recorded in release notes |
| FR-BACK-6 | Anthropic (Claude API) driver | W | P2 | — |

### Chats, folders, shortcuts, UI
| ID | Requirement | Pri | MS | Verification |
|---|---|---|---|---|
| FR-CHAT-1 | Create, rename, pin, archive, search (FTS) chats | M | M1 | unit + TUI pilot tests |
| FR-CHAT-2 | Folders (one level); move with `f` | M | M1 | TUI pilot + Playwright |
| FR-CHAT-3 | `d` deletes with confirm; `u` undoes; 30-day trash; purge | M | M1 | TUI pilot + Playwright |
| FR-UI-1 | TUI with all spec §4.6 shortcuts | M | M1 | `tests/e2e/tui/test_shortcuts.py` |
| FR-UI-2 | Web UI with the same screens and shortcuts | M | M1 | `tests/e2e/web/test_shortcuts.spec.ts` |
| FR-UI-3 | Single-key shortcuts can be turned off; keys rebindable | M | M1 | e2e tests with `single_key=false` |
| FR-UI-4 | Command palette (`Ctrl+K`), help overlay (`?`) | S | M1 | e2e |

### Effort, environments, context
| ID | Requirement | Pri | MS | Verification |
|---|---|---|---|---|
| FR-EFF-1 | Default effort levels low/medium/high mapped per spec §4.2 | M | M1 | `test_effort.py` |
| FR-EFF-2 | User-defined effort levels from `effort.toml` (all parameters in §4.2) | M | M1 | `test_effort_custom.py` |
| FR-ENV-1 | Environments from TOML; picker; switching changes tools/network/sandbox; switch is audited | M | M1 | `test_environments.py` |
| FR-ENV-2 | Built-in templates `coding`, `litreview-offline` (+ `litreview-online`, `scratch`: S) | M | M1 | unit |
| FR-CTX-1 | Context meter within ±5% of the backend's token count, broken down by category | M | M1 | `test_context_meter.py` against fake + real tokenizer |
| FR-CTX-2 | Auto-compaction at 80% with originals kept and searchable | M | M2 | integration + eval "compaction keeps key facts" |

### Uploads and artifacts
| ID | Requirement | Pri | MS | Verification |
|---|---|---|---|---|
| FR-UP-1 | Upload text/code, PDF, DOCX, CSV, images; hash audited | M | M1 | integration |
| FR-UP-2 | PDF Q&A answers cite page numbers | M | M2 | eval `evals/pdf_qa` |
| FR-UP-3 | Images go to the vision model (loaded on demand); clear message if none configured | M | M2 | integration with fake vision backend |
| FR-ART-1 | Artifacts in a git repo per chat; one commit per agent write | M | M1 | `test_artifacts_git.py` |
| FR-ART-2 | History, diff, restore (restore = new commit) | M | M2 | e2e |
| FR-ART-3 | External edits picked up and committed as `user` | S | M2 | integration |
| FR-ART-4 | HTML/SVG preview in sandboxed iframe on a separate origin, no network | M | M2 | Playwright: preview can't fetch or read parent |

### Tools, skills, coding
| ID | Requirement | Pri | MS | Verification |
|---|---|---|---|---|
| FR-TOOL-1 | MCP host (stdio); built-in servers `fs`, `shell`, `rag` | M | M2 | integration |
| FR-TOOL-2 | Tool permissions = manifest ∩ environment | M | M2 | `test_tool_permissions.py` |
| FR-TOOL-3 | Skills from SKILL.md folders; required permissions enforced | M | M2 | integration |
| FR-TOOL-4 | MCP pinning: version + hash; tool-definition hashes; change → disabled until re-approved | M | M2 | `tests/security/test_mcp_rugpull.py` |
| FR-CODE-1 | Agent proposes diffs; user approves per file (per hunk: P1) | M | M2 | e2e on `tests/fixtures/sample_repo` |
| FR-CODE-2 | Commands run in sandbox tier 1 (Linux) / tier 2 (Windows) with no network | M | M2 | `tests/security/test_sandbox_isolation.py` |
| FR-CODE-3 | Tier 4 off by default; when on, approval per command, badge, never for sub-agents/workflows | M | M2 | `test_sandbox_tier4_policy.py` |
| FR-CODE-4 | Sample task "fix the failing test" completes with ≤ 2 approvals | S | M2 | eval `evals/coding_fix_test` |

### Literature review and citations
| ID | Requirement | Pri | MS | Verification |
|---|---|---|---|---|
| FR-LIT-1 | Index a local PDF folder (Docling → chunks → embeddings in sqlite-vec) | M | M2 | integration |
| FR-LIT-2 | arXiv search (opt-in network), cached, audited | M | M3 | integration with recorded responses |
| FR-LIT-3 | Zotero read via local API | S | M3 | integration with fake Zotero API |
| FR-LIT-4 | Comparison table + `.bib` from 5 PDFs | M | M3 | eval `evals/litreview_table` |
| FR-LIT-5 | Semantic Scholar, OpenAlex, PubMed, Crossref | W | P2 | — |
| FR-CITE-1 | `cite.format` with CSL; APA and IEEE shipped; new `.csl` file adds a style | M | M3 | `test_cite_format.py` (golden outputs) |
| FR-CITE-2 | `citation-check`: every reference in a synthesis resolves (index/DOI) | M | M3 | eval `evals/citation_existence` (0 invented references) |

### Network, redaction, egress
| ID | Requirement | Pri | MS | Verification |
|---|---|---|---|---|
| FR-NET-1 | All outbound HTTP through the egress proxy; network off by default | M | M0 | `tests/security/test_no_egress_default.py` |
| FR-NET-2 | Host allowlist / ask per host; approval shows URL + redacted payload | M | M2 | integration |
| FR-NET-3 | SearXNG web search, opt-in | S | M3 | integration with fake SearXNG |
| FR-RED-1 | Secret scan blocks; PII scan asks; custom regexes; redactions audited by category + digest | M | M2 | `tests/security/test_redaction.py` |
| FR-EGR-1 | Per-chat egress report (host, bytes, redactions, decision, approver); export | M | M2 | e2e |

### Audit
| ID | Requirement | Pri | MS | Verification |
|---|---|---|---|---|
| FR-AUD-1 | Every effect has a pre- and post-audit record with actor, decision and digests | M | M0 | `test_audit_coverage.py` (every effect type) |
| FR-AUD-2 | Hash chain; `cairn audit verify` detects any edited/removed record | M | M0 | `test_audit_tamper.py` (Hypothesis) |
| FR-AUD-3 | Per-chat audit view, global browser, JSONL/CSV export | M | M2 | e2e |
| FR-AUD-4 | Signed checkpoints (Ed25519), exportable | S | M2 | unit |
| FR-AUD-5 | OpenTelemetry GenAI export to local file/collector, off by default | C | M3 | integration |
| FR-AUD-6 | Merkle checkpoints | W | P3 | — |

### Encryption and deletion
| ID | Requirement | Pri | MS | Verification |
|---|---|---|---|---|
| FR-ENC-1 | Lock chat / lock artifacts (separately) with passphrase; age format; key wrapping | M | M2 | `test_lock.py` |
| FR-ENC-2 | Passphrase entered only in client dialog; never in model context, tool args, logs or audit | M | M2 | `tests/security/test_passphrase_leak.py` (scan all stores/logs) |
| FR-ENC-3 | `encrypt`/`decrypt` are user-initiated only | M | M2 | `tests/security/test_user_only_actions.py` |
| FR-ENC-4 | Passphrase change re-wraps key only; recovery key optional | S | M2 | unit |
| FR-ENC-5 | Crypto-shredding: purge destroys the chat key; old DB copy can't be decrypted | M | M1 | `test_crypto_shred.py` |

### Policy
| ID | Requirement | Pri | MS | Verification |
|---|---|---|---|---|
| FR-POL-1 | `allow/ask/deny` decided outside the model for every effect | M | M0 | unit + coverage test |
| FR-POL-2 | Approval scopes: once / this chat / this environment | M | M1 | unit |
| FR-POL-3 | `user_initiated_only` actions (spec §5.8) rejected when triggered by non-user content | M | M2 | injection corpus `tests/security/injection/` |

### Agents and workflows
| ID | Requirement | Pri | MS | Verification |
|---|---|---|---|---|
| FR-AGENT-1 | `spawn_agent` with permission subset, nested audit | M | M3 | integration |
| FR-AGENT-2 | One YAML workflow (`lit-review`) runs end to end | S | M3 | integration with fake backend |
| FR-AGENT-3 | Budgets enforced: depth 2, tokens, tool steps, time; `max_parallel` per backend | M | M3 | `test_agent_budgets.py` |

### Connectors and sync
| ID | Requirement | Pri | MS | Verification |
|---|---|---|---|---|
| FR-SYNC-1 | Sync modes `none` (default) / `on_demand` / `auto` (on change, schedule) for every job | M | M3 | `test_sync_modes.py` |
| FR-SYNC-2 | Drive folder mirror (read-only) via rclone; preview for on-demand runs | S | M3 | integration with rclone local backend |
| FR-SYNC-3 | `backup` jobs via restic; restore to a date; retention | M | M3 | integration with local restic repo |
| FR-SYNC-4 | `replicate` jobs: per-device event logs + git artifacts; concurrent turns fork | M | M3 | Hypothesis merge-convergence test; two-install integration test |
| FR-SYNC-5 | Synced events validated against schema; treated as data | M | M3 | fuzz + unit |
| FR-SYNC-6 | Locked items synced only encrypted; "encrypt this sync target" option; cloud warning | M | M3 | `tests/security/test_sync_encryption.py` |
| FR-SYNC-7 | Agent can start (with approval) but not create/change sync jobs | M | M3 | covered by FR-POL-3 suite |

### Supply chain
| ID | Requirement | Pri | MS | Verification |
|---|---|---|---|---|
| FR-SUP-1 | `cairn doctor` checks external tools (Ollama, Podman/bwrap, git, restic, rclone) and explains fixes | M | M0 | unit + manual |
| FR-SUP-2 | Model downloads: GGUF/safetensors only, SHA-256 verified, audited | M | M2 | unit |
| FR-SUP-3 | Pickle-based model formats rejected | M | M2 | unit |

---

## Non-functional requirements

| ID | Requirement | Pri | Verification |
|---|---|---|---|
| NFR-P1 | UI reacts to input ≤ 100 ms (TUI and web) | M | e2e timing assertions |
| NFR-P2 | Core overhead ≤ 50 ms per turn excluding model time | M | benchmark test with fake backend |
| NFR-P3 | *Provisional:* main model first token ≤ 10 s for 2K-token prompt (warm) on the reference machine; fast model ≤ 3 s | S | `scripts/bench_backend.py` on reference machine |
| NFR-P4 | Warning before requests expected to need > 60 s of prompt processing (estimate from measured prompt speed) | M | unit with recorded benchmark profile |
| NFR-P5 | Cairn processes ≤ 1 GB RAM excluding model servers; core starts ≤ 3 s | S | CI measurement |
| NFR-R1 | No data loss on crash/power cut (WAL, atomic writes); interrupted turns resumable | M | kill-during-write tests |
| NFR-R2 | Audit record written before every effect | M | FR-AUD-1 test |
| NFR-S1 | Default install makes zero outbound connections | M | `test_no_egress_default.py` with network blocked |
| NFR-S2 | Threat model reviewed each phase; every T-xx has a control and test | M | review checklist in PR template |
| NFR-S3 | Local API resists CSRF and DNS rebinding | M | FR-API-3 tests |
| NFR-A1 | Web UI meets WCAG 2.2 AA | S | axe-core in Playwright; manual screen-reader pass |
| NFR-PORT-1 | Runs on Debian 12+, Ubuntu 22.04+, Windows 10/11; Python 3.12+ | M | CI matrix |
| NFR-OFF-1 | All non-network features work offline | M | e2e suite run with network blocked |
| NFR-SUP-1 | Hash-locked deps; pip-audit clean; license check; pinned CI actions | M | CI jobs |
| NFR-SUP-2 | Releases ship SBOM + Sigstore signatures + provenance | S | release workflow |
| NFR-MAINT-1 | ruff + mypy strict clean; ≥ 80% line coverage on core packages | M | CI |
