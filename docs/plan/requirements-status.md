# Requirements status

Every requirement in [requirements.md](../requirements.md), checked against the tests that exist
and the measurements, as of 2026-11-10. Current work and next steps are in the newest
`handoff-*.md` here.

**Key:**
- ✅ done, covered by automated tests (and CI), or measured on the reference machine where the row says so;
- 🧪 built, but checked only against a stand-in or fake, so a real check is pending (mostly on this machine);
- ◐ partly done;
- ☐ not done;
- ⏸ deferred or "Won't" by decision.

Some Verification cells in requirements.md describe the kind of test ("integration", "e2e"); the
real tests are named here.

### Privacy settings
| ID | Status | Notes |
|---|---|---|
| FR-PRIV-1 presets and overrides | ✅ | `test_privacy.py` |
| FR-PRIV-2 environment → global resolution | ✅ | `test_privacy.py` |
| FR-PRIV-3 `cairnward setup` | ✅ | `test_doctor_setup.py`; run for real on this machine |
| FR-PRIV-4 `privacy show`, badge | ✅ | CLI tests; dashboard badge in the browser test |
| FR-PRIV-5 settings changes audited | ✅ | `test_audit.py`; dashboard edits in `test_settings_edit.py` |
| FR-PRIV-6 each protection switchable | ✅ | per-preset tests in `test_privacy.py`, server modes in `test_server_modes.py` |

### Gateway and backends
| ID | Status | Notes |
|---|---|---|
| FR-GW-1 OpenAI-compatible endpoints | ✅ | `test_gateway.py`, `test_client_compat.py` |
| FR-GW-2 per-client keys | ✅ | `test_gateway.py` |
| FR-GW-3 virtual models `cw/<env>:<effort>` | ✅ | `test_gateway.py` |
| FR-GW-4 budgets | ✅ | `test_scheduling.py`: per run, per environment, and per client per day (fake model server only) |
| FR-GW-5 context warning | ✅ | `test_prompt_sizes.py` |
| FR-GW-6 Ollama-compatible endpoint | ⏸ | Won't (Phase 0) |
| FR-GW-7 prompt prefix never modified | ✅ | `test_gateway.py`, `test_vault_reconstruction.py` |
| FR-GW-8 affinity, keep-alive | ✅ | `test_scheduling.py` |
| FR-GW-9 llama.cpp KV-cache slots | 🧪 | built; tested with a fake server, not a real llama-server |
| FR-GW-10 memory check | ✅ | `test_scheduling.py`, `test_doctor_setup.py` |
| FR-GW-11 waiting-time estimate, `bench` | ✅ | `test_bench_backend.py` |
| FR-BACK-1 Ollama + OpenAI-compatible drivers | ◐ | real Ollama used on this machine; real llama-server not tried |
| FR-BACK-2 backends.toml, roles, override order | ✅ | `test_backend_resolution.py` |
| FR-BACK-3 fallback only to local | ✅ | `test_backend_fallback.py` |
| FR-BACK-4 hosted denied by default, ☁, locked never | ✅ | `test_hosted.py` |
| FR-BACK-5 tested hosted providers | 🧪 | templates done; no provider tried live |
| FR-BACK-6 Anthropic driver | ⏸ | Won't (Phase 0) |

### Isolation, egress, policy
| ID | Status | Notes |
|---|---|---|
| FR-ISO-1 `cairnward run` sandbox | ✅ | `tests/isolation/` (CI). The optional OpenShell backend (D77) is tested only against a stand-in CLI (`test_openshell_backend.py`) |
| FR-ISO-2 filesystem policy for runs | ✅ | `tests/isolation/test_run_home.py`, `test_run.py`; real Aider checked, Qwen Code only as a stand-in |
| FR-ISO-3 core isolated, only egress networked | ✅ | `test_isolated_serve.py`; running in isolated mode on this machine |
| FR-ISO-4 container templates for server clients | 🧪 | checked with `docker compose config` only; not run with Podman/Docker |
| FR-ISO-5 doctor + isolation self-test | ✅ | `test_doctor_setup.py`; passed on this machine |
| FR-NET-1 network off by default, allow-list, ask, logged | ✅ | `test_egress.py`, `test_tool_egress.py` |
| FR-NET-2 TLS interception | 🧪 | built (D78): `test_tls_inspection.py`, with real TLS, httpx and curl against a local HTTPS origin, on both the Python and the opt-in Rust data path (ADR-0020); path rules match every reading of the path (I36); not tried against outside sites or real agents |
| FR-EGR-1 egress report | ✅ | `test_egress_report.py` |
| FR-POL-1 allow/ask/deny outside the model | ✅ | `test_policy.py` |
| FR-POL-2 risk tiers | ✅ | `test_policy.py`, `test_approvals.py` |
| FR-POL-3 approvals inbox, batching | ✅ | `test_approvals.py`, browser test |
| FR-POL-4 user-only actions with provenance | ✅ | `security/test_user_only_actions.py` |
| FR-POL-5 approvals survive client retries | ✅ | `test_approvals.py` |

### MCP, agents, chat clients
| ID | Status | Notes |
|---|---|---|
| FR-MCP-1 MCP proxy | ✅ | `test_mcp.py`, `test_mcp_conformance.py` (official SDK both ways) |
| FR-MCP-2 manifest ∩ environment, sandboxed | ✅ | `test_mcp.py`, `isolation/test_mcp_sandbox.py` |
| FR-MCP-3 pinning, rug-pull detection | ✅ | `test_mcp.py` |
| FR-MCP-4 skills over MCP | ✅ | `test_mcp_builtins.py` |
| FR-AGT-1 agent profiles | ◐ | Aider, Qwen Code, opencode tested in the sandbox against a scripted model; **Goose untested**; coding eval with real models passes with Aider only (3 of 3 after the reasoning fix) |
| (remote MCP servers over HTTP, added) | ◐ | `test_mcp_remote.py` against a local SDK server; OAuth in `test_mcp_oauth.py` against a fake authorization server; no real server or provider tried, nor token refresh inside the sandbox |
| FR-AGT-2 run snapshots, diff, revert | ✅ | `test_snapshots.py` |
| FR-AGT-3 run linked in audit and vault | ✅ | `test_run.py` |
| FR-CLI-1 Open WebUI, Jan | 🧪 | setup guides and compatibility tests done; manual checklist (`docs/clients/checklist.md`) not done |
| FR-CLI-2 third client (LibreChat) | 🧪 | as above |

### Vault and encryption
| ID | Status | Notes |
|---|---|---|
| FR-VLT-1 store every exchange, per-conversation keys | ✅ | `test_store.py`, `test_gateway.py` |
| FR-VLT-2 rebuild conversations from stateless requests | ✅ | `test_vault_reconstruction.py` (property-based) |
| FR-VLT-3 vault browser with shortcuts | ✅ | browser test |
| FR-VLT-4 retention, `vault = "off"`/digests | ✅ | `test_vault_cli.py` |
| FR-ENC-1 lock with passphrase, recovery key | ◐ | conversations, their artifacts, finished jobs, run artifact repositories, research libraries and the recovery key (`security/test_lock.py`, `test_recovery_key.py`, `test_artifact_lock.py`, `test_item_lock.py`, `test_library_lock.py`); **run snapshots not covered** |
| FR-ENC-2 passphrase never leaks | ✅ | `security/test_lock.py`, `security/test_recovery_key.py` |
| FR-ENC-5 crypto-shredding on purge | ✅ | `test_store.py` |
| FR-ENC-6 database encryption | ✅ | built (D80, 2026-11-10): `test_sqlcipher.py` with the real SQLCipher driver; `tests/isolation/test_encrypted_storage.py` with `serve` in the real sandbox, isolated and not, checking no process has the key in its environment; reviewed (`docs/reviews/threat-model-2026-11-10.md`). Linux x86-64 only. Not tried with replication |

### Plugins
| ID | Status | Notes |
|---|---|---|
| FR-PLUG-1 sandbox required, no network | ✅ | `isolation/test_plugin_contract.py` in all three server modes |
| FR-PLUG-2 fetch service | ✅ | real egress and local origin; a real outside host only in the owner-side checks |
| FR-PLUG-3 per-launch key | ✅ | `test_plugin_contract.py` |
| FR-PLUG-4 pinned code | ✅ | `isolation/test_plugin_contract.py` |
| FR-PLUG-5 approval after plugin output | ✅ | `test_plugin_contract.py`, with a scripted approver; not seen in a chat client. An agent's own network requests aren't covered |
| FR-PLUG-6 conformance suite | ✅ | `isolation/test_plugin_contract.py`; only the research plugin and a test plugin have run it |

Research requirements (FR-RES-*) are tracked in the `literature-review-agent-plugin` repository.

### Artifacts, audit, redaction
| ID | Status | Notes |
|---|---|---|
| FR-ART-1 git repo per conversation/job/run | ✅ | `test_artifacts_git.py` |
| FR-ART-4 sandboxed HTML/SVG previews | ✅ | browser test |
| FR-AUD-1 pre/post records for every effect | ✅ | `test_audit.py` |
| FR-AUD-2 hash chain, `audit verify` | ✅ | `test_audit.py` |
| FR-AUD-3 audit browser, export | ✅ | browser test, `test_audit.py` |
| FR-AUD-4 signed checkpoints to a private git repo | ✅ | `test_anchoring.py` (local bare repo, real `ssh-keygen` signatures); since 2026-11-10 each checkpoint also signs a Merkle root (D79); a real private remote not set up yet |
| FR-AUD-5 OpenTelemetry export | ✅ | `test_otel.py` (mock collector) |
| FR-AUD-6 Merkle proofs and an independent verifier | ✅ | built (D79, D81, 2026-11-10): `test_merkle.py` (RFC 6962 reference roots, Hypothesis), `test_anchoring.py` (`audit prove` / `check-proof` end to end), `test_audit_rust.py` (the Rust verifier agrees with Python on generated records and catches the same tampering) |
| (RFC 3161 timestamps, added) | ✅ | `test_timestamp.py` (real `openssl ts` authority); no public TSA tried |
| FR-RED-1 built-in scanner | ✅ | `security/test_redaction.py` |
| FR-RED-2 Presidio | ✅ | checked with the real package; small model misses some names |

### Sync and backup
| ID | Status | Notes |
|---|---|---|
| FR-SYNC-1 none/on_demand/auto | ✅ | `test_backup.py` (backups; replication deferred) |
| FR-SYNC-2 Drive mirror (rclone) | 🧪 | `test_drive.py` (stand-in rclone) |
| FR-SYNC-3 restic backups, retention, restore | ✅ | `test_backup.py` with real restic to a folder; **remote repository not tried** |
| FR-SYNC-4 multi-device replication | 🧪 | `test_replication.py`: real encryption, SQLite and git, devices as separate folders on one machine; **not tried between two machines or through real Syncthing** |
| FR-SYNC-6 locked items only encrypted; cloud warning | ✅ | backups and the shared folder are always encrypted; cloud warning for both (`test_backup.py`, `test_replication.py`) |
| FR-SYNC-8 purge rewrites snapshots | ✅ | `test_backup.py` |

### Dashboard and supply chain
| ID | Status | Notes |
|---|---|---|
| FR-DASH-1 loopback, Host/Origin/CSRF, login link | ✅ | `security/test_local_api.py` |
| FR-DASH-2 all views | ✅ | approvals, activity, audit, egress, vault, jobs, artifacts, backups (with other devices), MCP, settings |
| FR-DASH-3 / NFR-A1 WCAG 2.2 AA | ◐ | axe-core: 0 violations on all views and with dialogs open, light and dark; focus checked in dialogs; a manual check not done |
| FR-SUP-2 model downloads verified | ✅ | `test_model_files.py` |

### Non-functional
| ID | Status | Notes |
|---|---|---|
| NFR-P2 gateway overhead ≤ 75 ms, ≤ 150 ms isolated | ✅ | `scripts/bench_gateway.py`: Strict 105 ms isolated, 59 ms not; all off 7 ms. Target raised from 50 ms by owner decision, 2026-11-05 |
| NFR-P3 cache reuse ≥ 10× | ✅ | measured through the gateway: 19.6× |
| NFR-P5 core ≤ 500 MB, starts ≤ 3 s | ◐ | measured: 122 MB meets. Start was about 3.5 s isolated on the desktop on 2026-11-05 (misses). After the I15 changes, a cloud session measured 2.1–2.2 s (empty data) and 1.9–2.3 s (existing data), down from 2.6–2.9 s on the same machine; not yet re-measured on the desktop (`scripts/bench_startup.py`) |
| NFR-P6 first token within 15 s / 30 s | ✅ | measured through the gateway, model loaded: 11 to 14 s fast, 22 s main; loading the main model first adds about 70 s |
| NFR-R1 no data loss, jobs resumable | ✅ | `test_jobs_resume.py`, atomic writes |
| NFR-R2 audit before every effect | ✅ | as FR-AUD-1 |
| NFR-S1 Strict: no outbound unless allow-listed | ✅ | isolation suite, `test_egress.py` |
| NFR-S2 threat model reviewed each milestone | ◐ | reviews recorded in `docs/reviews/` (latest 2026-11-10: path rules, database encryption, the Rust data path; threat model v0.7); the PR template prompts for more; **no review yet by anyone but the assistant that wrote the code** |
| NFR-S4 isolation holds for every sandbox type | ✅ | isolation suite in CI |
| NFR-PORT-1 Linux; unit tests on Windows | ✅ | CI (Ubuntu, Debian, Windows) |
| NFR-PORT-2 WSL2 | 🧪 | D74: detection, hiding Windows drives and programs, doctor checks (`test_wsl.py`, `isolation/test_wsl_sandbox.py`: stand-in `/proc` and folders in the real sandbox); **never run on real WSL2**; checklist in `docs/wsl2.md` |
| NFR-SUP-1 hash-locked deps, audits, licence check, pinned actions | ✅ | hash locks, pip-audit, npm audit and pinned actions done; licence allow-list in CI's audit job (runtime Python dependencies; research engines and npm packages not covered) |
| NFR-SUP-2 SBOM + signed releases | 🧪 | `release.yml`, `test_release.py` (real build and SBOM); signing and the draft release have never run on GitHub |
| NFR-MAINT-1 ruff, mypy strict, ≥ 80% coverage | ✅ | every counted package at 80% or more, enforced by `scripts/check_coverage.py` in CI; 78% over all of `src/`; a short list is left out by name, including `core/server.py` |
