# Cairnward requirements

Status: Draft v0.4 (goes with spec v0.8. V0.3 went with spec v0.7: all protections optional. V0.1 described the all-in-one design and is in git history)

All security requirements below describe what each protection does **when it is switched on**. FR-PRIV-* require that each can be switched off.

- **IDs** are stable: never reuse or renumber. v0.1 IDs that no longer apply are listed at the end as withdrawn.
- **Priority (MoSCoW) for Phase 0:** **M** Must, **S** Should (may slip to 0.x), **C** Could, **W** Won't in Phase 0.
- **MS:** milestone M0–M3 from [plan/phase0.md](plan/phase0.md). L = later.
- **Verification:** the test proving it. Every **M** item needs an automated test before Phase 0 closes.

---

## Functional requirements

### Privacy settings and presets
| ID | Requirement | Pri | MS | Verification |
|---|---|---|---|---|
| FR-PRIV-1 | `[privacy]` settings (spec §5.0 table) with presets Open / Balanced / Strict. Individual overrides mark *Custom* | M | M0 | `test_privacy.py` |
| FR-PRIV-2 | Resolution environment → global. Virtual models carry the environment's privacy level. `cairnward run --env` applies it | M | M0 | `test_privacy.py` |
| FR-PRIV-3 | `cairnward setup` first-run preset choice + prerequisite check. Unmet prerequisites shown as *requested but inactive* | M | M0 | integration |
| FR-PRIV-4 | `cairnward privacy show [--env]`, status badge in CLI and dashboard. Warnings for ineffective combinations (e.g. allowlist without isolation = advisory) | M | M0 (CLI), M2 (dashboard) | unit + e2e |
| FR-PRIV-5 | Every settings change audited. `audit.disabled` written before audit is switched off, `audit.enabled` with gap duration when back on | M | M0 | `test_audit.py`, `test_settings_edit.py` |
| FR-PRIV-6 | Each protection can be switched off independently and Cairnward keeps working. Full test suite passes under Open, Balanced, Strict and all-off | M | M0 onward | CI matrix over presets |

### Gateway: model endpoint and backends
| ID | Requirement | Pri | MS | Verification |
|---|---|---|---|---|
| FR-GW-1 | OpenAI-compatible `/v1/chat/completions` (streaming, tool calls), `/v1/models`, `/v1/embeddings` | M | M0 | integration tests with fake backends. Conformance against the OpenAI SDK |
| FR-GW-2 | Per-client API keys (`cairnward client add`). Requests attributed to a client in audit/vault | M | M0 | `test_gateway.py` |
| FR-GW-3 | Virtual models `cw/<environment>:<effort>` resolve environment + effort settings | M | M1 | `test_gateway.py` |
| FR-GW-4 | Per-client, per-run and per-environment budgets (tokens, requests, time) | M | M2 | `test_scheduling.py` — ✅ runs (requests, tokens, minutes). Clients (requests and tokens per day) |
| FR-GW-5 | Context warning: request expected to need > 60 s prompt processing is flagged (dashboard + optional rejection) | S | M2 | unit with benchmark profile — ✅ |
| FR-GW-6 | Ollama-compatible endpoint | W | L | — |
| FR-GW-7 | Gateway never modifies a conversation's prompt prefix (additions go at the end) | M | M0 | `test_gateway.py`, `test_vault_reconstruction.py` (byte-identical prefixes across turns) |
| FR-GW-8 | Conversation affinity, keep-alive while active, unload after idle | M | M1 | integration with fake backend tracking cache hits — ✅ affinity queue + keep-alive |
| FR-GW-9 | KV-cache save/restore per conversation with llama.cpp slots | S | M2 | integration with llama-server |
| FR-GW-10 | Memory check before loading a model (size + context vs available RAM). Warn or queue | M | M1 | unit with mocked meminfo |
| FR-GW-11 | Waiting-time estimate from stored benchmark profile. `cairnward bench` imports/re-runs the profile | M | M1 | unit — ✅ `cairnward bench import` |
| FR-BACK-1 | Ollama and OpenAI-compatible backend drivers (streaming, tools, embeddings, health) | M | M0 | integration with fake servers. Manual against real Ollama + llama-server |
| FR-BACK-2 | Backends in `backends.toml`. Model per role. Override order run → client → environment → global | M | M1 | `test_backend_resolution.py` |
| FR-BACK-3 | Fallback only to local backends, logged | M | M1 | `test_backend_fallback.py` |
| FR-BACK-4 | Hosted backends denied by default. Never for locked conversations unless overridden. ☁ marking | M | M3 | `test_hosted.py` |
| FR-BACK-5 | Tested hosted providers: Alibaba Model Studio, DeepSeek API, OpenRouter | S | M3 | manual, release notes |
| FR-BACK-6 | Anthropic driver (Claude API key) | W | L | — |

### Isolation and egress (OS-enforced)
| ID | Requirement | Pri | MS | Verification |
|---|---|---|---|---|
| FR-ISO-1 | `cairnward run -- <cmd>` runs a command under bubblewrap with a loopback-only network namespace. Gateway, MCP and proxy reachable via bridged sockets | M | M0 | `tests/isolation/` (direct TCP/UDP/DNS fail. Gateway works) |
| FR-ISO-2 | Filesystem policy for runs: workspace rw, agent config rw, home folder hidden except what the run needs, secrets dirs hidden always, rest read-only | M | M0 | `tests/isolation/`, `test_run.py` |
| FR-ISO-3 | Cairnward core runs in its own loopback-only namespace. Only `cairnward-egress` has network | M | M0 | `tests/isolation/test_isolated_serve.py` |
| FR-ISO-4 | Container templates for server clients on an internal-only network | M | M2 | integration (Podman) |
| FR-ISO-5 | `cairnward doctor` checks prerequisites and runs isolation self-tests | M | M0 | unit + isolation suite |
| FR-NET-1 | Network off by default. Host allowlist / ask per host. Every connection logged (host, bytes, timing, client/run) | M | M0 | `test_egress.py`, `tests/isolation/test_tool_egress.py` |
| FR-NET-2 | Optional TLS interception with local CA, off by default, per environment | C | L | — |
| FR-EGR-1 | Egress report per conversation, run and client. Export | M | M2 | e2e |

### Policy and approvals
| ID | Requirement | Pri | MS | Verification |
|---|---|---|---|---|
| FR-POL-1 | `allow/ask/deny` decided outside the model for every effect | M | M0 | unit + coverage test |
| FR-POL-2 | Risk tiers (low → allow+log, medium → ask once per scope, high → always ask) | M | M2 | `test_policy.py`, `test_approvals.py` |
| FR-POL-3 | Approvals inbox (dashboard, desktop notification, `cairnward approvals`). Batching of similar requests | M | M2 | e2e |
| FR-POL-4 | User-only actions open a confirmation dialog in Cairnward's UI showing action + provenance. Passphrases entered only there | M | M2 | `tests/security/test_user_only_actions.py` + injection corpus |
| FR-POL-5 | Approvals survive client retries after timeouts | S | M2 | integration |

### MCP proxy and tools
| ID | Requirement | Pri | MS | Verification |
|---|---|---|---|---|
| FR-MCP-1 | MCP proxy (streamable HTTP + stdio shim) aggregating enabled servers per environment | M | M2 | integration |
| FR-MCP-2 | Permissions = manifest ∩ environment. Third-party servers sandboxed | M | M2 | `tests/test_mcp.py`, `tests/isolation/test_mcp_sandbox.py` |
| FR-MCP-3 | Version/hash pinning. Tool-definition hashes. Change → disabled until re-approved | M | M2 | `tests/test_mcp.py::test_rug_pull_disables_the_tool` |
| FR-MCP-4 | Skills (SKILL.md) served to clients | S | M2 | integration |

### Coding agents
| ID | Requirement | Pri | MS | Verification |
|---|---|---|---|---|
| FR-AGT-1 | Profiles for Aider (first, M0), Qwen Code, opencode, Goose + a generic OpenAI-compatible profile. Each profile records its typical prompt size and is flagged when it exceeds the virtual model's context or the 60 s threshold | M | M2 (Aider in M0) | compatibility tests with fake model server — ✅ Aider, Qwen Code, opencode tested. Goose untested |
| FR-AGT-2 | Workspace snapshots before/after each run on hidden refs. `runs diff`, `runs revert` | M | M1 | `test_snapshots.py` — ✅ |
| FR-AGT-3 | All model/tool calls of a run linked to the run in audit and vault | M | M0 | integration |

### Chat clients
| ID | Requirement | Pri | MS | Verification |
|---|---|---|---|---|
| FR-CLI-1 | Tested setup guides + profiles for Open WebUI and Jan | M | M2 | compatibility tests. Manual checklist |
| FR-CLI-2 | Third tested client (LibreChat or AnythingLLM) | S | M2 | same |

### Transcript vault
| ID | Requirement | Pri | MS | Verification |
|---|---|---|---|---|
| FR-VLT-1 | Store every request/response through the model endpoint, per-conversation age keys | M | M0 | integration |
| FR-VLT-2 | Reconstruct conversations from stateless requests by prefix hashing. Branches for divergence | M | M1 | Hypothesis property test |
| FR-VLT-3 | Vault browser: search, folders, pin, export, delete with confirm/undo. Shortcuts (`d`, `f`, `/`, `j/k`, `L`, `x`, `l`, `?`). Single-key shortcuts can be turned off | M | M2 | Playwright e2e |
| FR-VLT-4 | Per-environment retention and `vault = "off"` (digests only) | M | M2 | unit |
| FR-ENC-1 | Lock conversation / artifacts with passphrase (age, key wrapping, recovery key optional) | M | M2 | `test_lock.py`, `test_recovery_key.py`, `test_artifact_lock.py`, `test_item_lock.py`, `test_library_lock.py`. Run snapshots not covered |
| FR-ENC-2 | Passphrase never in model context, tool args, logs or audit | M | M2 | `tests/security/test_lock.py`, `tests/security/test_recovery_key.py` |
| FR-ENC-5 | Crypto-shredding on purge | M | M1 | `test_store.py` |
| FR-ENC-6 | Optional whole-database encryption (vault, keys, audit) with a passphrase-wrapped key. A key reference for unattended starts is a visible weakening (D80) | C | M3 | `test_sqlcipher.py`, `tests/isolation/test_encrypted_storage.py` |

### Research workflows
FR-RES-1 to FR-RES-13 moved to the `literature-review-agent-plugin` repository with ADR-0017 (D75).

### Plugins
| ID | Requirement | Pri | MS | Verification |
|---|---|---|---|---|
| FR-PLUG-1 | A plugin has no network of its own and needs the sandbox whatever the environment says. Running it unsandboxed is a separate switch, warned about and audited | M | M3 | `isolation/test_plugin_contract.py`, `test_plugin_contract.py` |
| FR-PLUG-2 | Fetch service: destination checked, outgoing text scanned, sent through egress, audited as a digest, limited to declared hosts | M | M3 | `isolation/test_plugin_contract.py` (real egress and local origin), `test_plugin_contract.py` |
| FR-PLUG-3 | Per-launch key: in memory, revoked at stop, pinned to one environment, no use of the MCP endpoint | M | M3 | `test_plugin_contract.py` |
| FR-PLUG-4 | Installed code pinned by hash. A change stops the plugin until reviewed | M | M3 | `isolation/test_plugin_contract.py` |
| FR-PLUG-5 | Actions that follow a plugin's output need approval naming the plugin | M | M3 | `test_plugin_contract.py` |
| FR-PLUG-6 | A plugin must pass the conformance suite for its code and contract version before it starts | M | M3 | `isolation/test_plugin_contract.py` |

### Artifacts
| ID | Requirement | Pri | MS | Verification |
|---|---|---|---|---|
| FR-ART-1 | Git repo per conversation/job/run. One commit per write. Restore = new commit | M | M1 | `test_artifacts_git.py` |
| FR-ART-4 | HTML/SVG previews on a sandboxed origin without network | M | M2 | Playwright |

### Audit
| ID | Requirement | Pri | MS | Verification |
|---|---|---|---|---|
| FR-AUD-1 | Pre- and post-records for every effect with actor (client/agent/run/user), decision, digests | M | M0 | `test_audit.py` |
| FR-AUD-2 | Hash chain. `audit verify` detects edits/removals | M | M0 | Hypothesis tamper tests |
| FR-AUD-3 | Audit browser (per client/run/conversation), export | M | M2 | e2e |
| FR-AUD-4 | Signed checkpoints anchored hourly + on demand to a private git repo (deploy key scoped to it). `audit verify --anchors` | M | M2 | `test_anchoring.py` with local bare repo |
| FR-AUD-5 | OpenTelemetry GenAI export (local), off by default | C | M3 | integration |
| FR-AUD-6 | Merkle checkpoints: single-record inclusion proofs checkable without the log. An independent verifier for exported logs (D79, D81) | C | M3 | `test_merkle.py`, `test_anchoring.py`, `test_audit_rust.py` |

### Redaction
| ID | Requirement | Pri | MS | Verification |
|---|---|---|---|---|
| FR-RED-1 | Built-in scanner (secrets, emails, phones, ID numbers, custom regex) on traffic Cairnward terminates. Secrets block, PII ask | M | M2 | `tests/security/test_redaction.py` |
| FR-RED-2 | Presidio as optional worker | S | M3 | integration |

### Sync and backup
| ID | Requirement | Pri | MS | Verification |
|---|---|---|---|---|
| FR-SYNC-1 | Modes none/on_demand/auto for every job | M | M3 | `test_backup.py` |
| FR-SYNC-3 | restic backups of vault, artifacts, plugins' data. Retention. Restore to date | M | M3 | local restic repo |
| FR-SYNC-4 | Replication via per-device event logs + git history for artifacts, through a shared folder. Deterministic merge. Fork on concurrent turns | S | M3 | Hypothesis convergence (`test_replication.py`) |
| FR-SYNC-6 | Locked items only written encrypted. Cloud warning. "encrypt this sync target" | M | M3 | `test_backup.py` (backups encrypted, cloud warning) |
| FR-SYNC-8 | Purge rewrites restic snapshots and prunes. Pending state shown. Retries | M | M3 | `test_backup.py` (content absent from all snapshots) |
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
| NFR-P2 | Gateway adds ≤ 75 ms, or ≤ 150 ms with isolation per request excluding model time (Strict). ≤ 10 ms with all protections off. Raised from 50 ms on 2026-11-05 by owner decision: the cost is audit writes waiting for the disk and the sandbox's route to the model, and a model call takes minutes on the reference machine | M | `scripts/bench_gateway.py`, `test_bench_gateway.py`. Strict 105 ms isolated, 59 ms not (meets), all off 7 ms (meets) |
| NFR-P3 | Follow-up turns reuse the prompt cache: ≥ 10× faster first token than cold (reference machine measured 18.6×) | M | `bench_backend.py --cache-test` through the gateway: 19.6× |
| NFR-P6 | 512-token prompt: first token ≤ 15 s on the fast model, ≤ 30 s on the main model (reference machine) | S | `bench_backend.py` through the gateway: 11 to 14 s fast, 22 s main |
| NFR-P5 | Core ≤ 500 MB RAM without optional workers. Starts ≤ 3 s | S | `docs/bench/results-2026-11-03-measurements.md`: 122 MB meets. Start about 3.5 s isolated on 2026-11-05, misses. After the I15 changes about 0.5 to 0.9 s faster on another machine, not yet re-measured on the reference machine (`scripts/bench_startup.py`). See `docs/status.md` I15. |
| NFR-R1 | No data loss on crash | M | kill tests |
| NFR-R2 | Audit record before every effect | M | FR-AUD-1 |
| NFR-S1 | Strict: zero outbound connections unless allowlisted. Balanced: none without approval | M | isolation suite per preset |
| NFR-S2 | Threat model reviewed each milestone | M | PR template. Recorded in `docs/reviews/` |
| NFR-S4 | With isolation on, isolation holds for every sandbox type (direct network attempts fail) | M | isolation suite in CI (Linux) |
| NFR-A1 | Dashboard WCAG 2.2 AA | S | axe-core |
| NFR-PORT-1 | Linux (Debian 12+, Ubuntu 22.04+). Python 3.12+. Unit tests also pass on Windows | M | CI |
| NFR-PORT-2 | Windows via WSL2 | S | `tests/test_wsl.py`, `tests/isolation/test_wsl_sandbox.py`. Real machine: checklist in `docs/wsl2.md` |
| NFR-SUP-1 | Hash-locked deps. pip-audit. License check. Pinned CI actions | M | CI |
| NFR-SUP-2 | SBOM + Sigstore-signed releases | S | `.github/workflows/release.yml`, `test_release.py`. Never run on GitHub yet |
| NFR-MAINT-1 | ruff + mypy strict. ≥ 80% coverage in every package (a short list is left out by name, each with a reason, in `scripts/check_coverage.py`) | M | CI (Linux) |

---

## Withdrawn (from v0.1)
- FR-API-1–5 → FR-GW-*, FR-DASH-1
- FR-CHAT-1–3, FR-UI-1–4 (own chat clients) → FR-VLT-3, FR-CLI-*
- FR-EFF-*, FR-ENV-* → FR-GW-3 (definitions unchanged)
- FR-CTX-1–2 → FR-GW-5 and client `usage` reporting
- FR-UP-* (uploads via client) → client feature
- FR-CODE-1–4 (own coding flow) → FR-AGT-*
- FR-TOOL-1–4 → FR-MCP-*
- FR-LIT-*, FR-CITE-* → FR-RES-*
- FR-AGENT-* → FR-RES-11
- FR-ENC-3 → FR-POL-4
- FR-SYNC-5, FR-SYNC-7 → covered by FR-SYNC-4 and FR-POL-4
- FR-SUP-1 → FR-ISO-5
- FR-SUP-3 → FR-SUP-2
- NFR-P1, NFR-P4, NFR-S3, NFR-OFF-1 → NFR-P2, FR-GW-5, FR-DASH-1, NFR-S1
