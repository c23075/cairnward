# Cairnward threat model

Status: Draft v0.3, reviewed 2026-11-03 ([review](reviews/threat-model-2026-11-03.md); earlier versions in git history)
Method: **STRIDE** (security) and **LINDDUN** (privacy) applied to the components and data flows in [spec §3](spec.md#3-architecture), with every control mapped to the **OWASP Top 10 for LLM Applications (2025)**. Each threat has an ID; controls point to spec sections and requirement IDs in [requirements.md](requirements.md).

Review this document whenever a component, data flow or trust boundary changes, and at least once per milestone. Record each review in [`reviews/`](reviews/). A control marked *planned* doesn't exist yet.

**Assumed configuration:** the **Strict** preset (spec §5.0). Every protection is optional; a control listed here applies only when its setting is on. `cairnward privacy show` tells you which controls are active in each environment.

---

## 1. Assets

| ID | Asset | Why it matters |
|---|---|---|
| A1 | Chat content (messages, attachments, memory) | Private research, code, personal data |
| A2 | Artifacts | Work product; may contain the above |
| A3 | Workspace files and repos | Source code, unpublished papers |
| A4 | Credentials (API keys, OAuth tokens, restic password, passphrases, chat keys) | Access to cloud accounts and encrypted data |
| A5 | Audit log | Evidence of what the agent did; also sensitive itself (a record of activity) |
| A6 | Host system integrity | The agent runs commands |
| A7 | Model files | Code-execution risk if a format allows it |

## 2. Trust boundaries

| ID | Boundary | Trusted side | Untrusted side |
|---|---|---|---|
| B1 | Browser ↔ core API (loopback) | Core | Any web page the user visits (can send requests to `localhost`) |
| B2 | Core ↔ model output | Core, policy engine | **Model output** (can be steered by injected content) |
| B3 | Core ↔ content sources | Core | Web pages, PDFs, repo files, tool results, Drive files, synced events from other devices |
| B4 | Core ↔ sandbox / MCP servers and plugins | Core | Commands, scripts, third-party MCP servers, plugins |
| B5 | Core ↔ network (egress proxy) | Core | Internet hosts, hosted model backends, sync targets |
| B6 | Core ↔ local disk | Core while running | Anyone with later access to the disk or a backup |
| B7 | Core ↔ third-party clients and agents | Core | Chat clients and coding agents (Open WebUI, Jan, Qwen Code, opencode, Aider, Goose, ...): their code, updates and own network behaviour |

The model is **not** a trusted component: anything it decides passes through the policy engine (B2). Since v0.6, third-party clients and agents are **not trusted** either (B7): they run in OS-enforced sandboxes and only reach the network through `cairnward-egress`.

## 3. STRIDE threats

| ID | STRIDE | Threat | Controls | Spec | Verified by |
|---|---|---|---|---|---|
| T-01 | Spoofing | A malicious web page sends requests to the local dashboard or gateway (CSRF / DNS rebinding) | Unix socket for CLI; Host allowlist; Origin check; SameSite cookie + CSRF token; no CORS; per-client API keys on the gateway | §3.1 | FR-DASH-1, FR-GW-2 |
| T-02 | Spoofing | Another local process uses the gateway as if it were a configured client | Per-client API keys; socket/token file mode 0600 | §3.1 | FR-GW-2 |
| T-03 | Tampering | Audit records edited or deleted after the fact | Hash chain; signed checkpoints **anchored to a private git repo (hourly by default)**; `audit verify --anchors`. Residual: records newer than the last anchor can be altered by someone controlling the machine (visible as a missing/late anchor) | §5.2 | FR-AUD-2, FR-AUD-4 |
| T-04 | Tampering | MCP server changes its tool descriptions after approval (rug pull) or ships hidden instructions (tool poisoning) | Version + hash pinning; tool-definition hashes; disable on change; description scan; sandboxed servers | §4.2 | FR-MCP-3 |
| T-05 | Tampering | Synced events from another device, or someone with access to the shared folder, are malformed or malicious | Each record encrypted, signed by its device and bound to its device, file and position; schema validation; events are data, never instructions; a new device's changes need approval; only ordinary files accepted in artifact history. **Not covered:** an approved device that turns malicious can write bad records under its own name, until blocked | §4.8 | FR-SYNC-4 |
| T-06 | Repudiation | Unclear whether the user, a client, an agent run or a sub-agent caused an action | `actor` (client/run/agent/user) + approving user action recorded on every effect | §5.2 | FR-AUD-1 |
| T-07 | Info disclosure | Prompt injection, a misbehaving library or a third-party tool sends data out | **OS-enforced** loopback-only namespaces; only `cairnward-egress` has network; host allowlist; redaction on terminated traffic; provenance in approval dialogs | §3.2, §5.3–5.4 | NFR-S1, NFR-S4, FR-NET-1 |
| T-08 | Info disclosure | Secrets or personal data sent to a hosted model or search engine | Redaction (secrets block, PII ask); hosted backends denied by default; locked chats never sent to hosted backends | §4.1, §5.4 | FR-RED-1, FR-BACK-4 |
| T-09 | Info disclosure | Disk, laptop or backup stolen | Opt-in chat locking with age, and locking of a conversation's artifacts; restic backups always encrypted; full-disk encryption recommended at first run | §5.5 | FR-ENC-1 |
| T-10 | Info disclosure | "Deleted" conversations recovered from SSD remnants, old copies or backups | Crypto-shredding; **purge rewrites restic snapshots**; `secure_delete` + VACUUM for the plaintext index (best effort, stated) | §4.5, §4.8 | FR-ENC-5, FR-SYNC-8 |
| T-11 | Info disclosure | Artifact preview (HTML/SVG) runs scripts that read Cairnward data or phone home | Sandboxed iframe with an opaque origin; CSP with no network and no scripts | §4.7 | FR-ART-4 |
| T-12 | Info disclosure | A coding agent or its commands read files outside the workspace (keys, other projects) | bubblewrap filesystem policy: workspace rw, agent state folders rw, the whole home folder hidden except the program, its runtime and git config (`home_access = "hidden"`, on in Balanced and Strict); known secret folders and Cairnward's own folders, the backup folder and the replication folder hidden even inside what is re-allowed; workspace snapshots for review/revert. Residual: `~/.gitconfig` (a token in a URL there is readable), the agent's own state folder, and for most profiles `~/.cache` apart from browser and Cairnward caches. A workspace that contains the home folder gets the known-secrets list only, with a warning. The core, MCP servers without a `read` list and the research engines still use the list of known secret folders, which can't name everything | §3.2, §4.3 | FR-ISO-2, FR-AGT-2 |
| T-13 | Denial of service | Runaway agent loop, sub-agent explosion or huge prompts lock the machine | Per-client (daily), per-run and per-environment budgets at the gateway; context warnings. Workflows have budgets, `max_papers` and a capped `max_parallel` | §4.1, §4.6 | FR-GW-4, FR-RES-11 |
| T-14 | Elevation of privilege | Injected content triggers user-only actions (encrypt everything, add sync target, enable hosted backend) | Confirmation dialog **in Cairnward's own UI** (not the client) showing provenance, with untrusted content since the last user message highlighted; passphrases only typed there. Residual: a user may approve without reading (accepted, mitigated by risk tiers keeping these prompts rare) | §5.3 | FR-POL-4 |
| T-15 | Elevation of privilege | Malicious model file runs code on load | GGUF/safetensors only; checksums; downloads via egress and audited | §5.6 | FR-SUP-2 |
| T-16 | Elevation of privilege | Compromised dependency or release | Hash-locked deps; pip-audit; licence allow-list; pinned CI actions. Release workflow with SBOM and signing is built but has never run on GitHub | §5.6 | NFR-SUP-1 |
| T-17 | Info disclosure | Third-party tool sends sensitive content over HTTPS to an allowed host; Cairnward can't see content to redact | Network off by default; per-host approval; egress report with bytes per host; optional TLS interception (off). Accepted limitation | §3.2 | FR-NET-1, FR-EGR-1 |
| T-18 | Info disclosure | Conversation copies persist in a client's own database outside Cairnward's control (no lock, no purge) | Setup guides minimise client history; vault UI states it; clients run in containers whose data volumes the user controls | §4.4, §4.5 | FR-CLI-1 |
| T-19 | Tampering / EoP | Sandbox misconfiguration silently gives a tool network or file access | `cairnward doctor` isolation self-tests at start and on demand; CI isolation suite | §3.2 | FR-ISO-5, NFR-S4 |
| T-20 | Spoofing | Compromised or malicious client update (e.g. a chat app adds telemetry) | Client runs sandboxed with network only via egress; new hosts require approval and appear in the egress report | §3.2 | FR-ISO-4, FR-NET-1 |
| T-21 | Elevation of privilege | A sandboxed agent asks for a host that is really this machine (`localhost`, `127.1`, a metadata address) to reach local services (Ollama, other apps) around egress, since the sandbox connects to loopback directly | The core applies egress's address guard to sandbox asks: such addresses are refused unasked unless listed exactly; names that resolve to them later still go through egress and its DNS check | §3.2, §5.3 | FR-NET-1 |
| T-22 | Tampering / Info disclosure | A remote MCP server returns poisoned tools, or receives data it shouldn't | Reached through egress under the environment's host rules; tools pinned like a local server's; bearer token from an environment variable. OAuth tokens stored and refreshed by Cairnward. Tested against a local SDK server and a fake authorization server only | §4.2 | FR-MCP-1, FR-MCP-3 |
| T-23 | Info disclosure | A steered model puts private text in an academic search query (`lit_find`) | Sources opt-in per environment; allow-listed hosts; audited as digests. Residual: the query leaves the machine, and egress can't read it | §4.6 | FR-RES-8, FR-RES-9 |
| T-24 | Info disclosure | The research library is plain text in `research.db` unless locked; locking a conversation doesn't cover a library it used | Owner-only data folder; encrypted in backups; `cairnward-research library lock` seals its text, embeddings and metadata. Residual: the source PDFs stay in your folder, a library is plain while unlocked, and older backups hold it | §4.6 | FR-RES-1 |
| T-25 | Elevation of privilege | A crafted PDF exploits a third-party engine, or PaperQA2's `pickle` cache is altered | Since ADR-0017, parsing and these workers run in the research plugin's sandbox, not the core. PaperQA2 and Docling have their own environment; PaperQA2 refuses settings that don't point at the gateway; Presidio runs inside the core's sandbox with no network; cache folder is owner-only and hidden from agent runs, though profiles may write the rest of `~/.cache` | §4.6 | FR-RES-13, FR-RED-2 |
| T-26 | Tampering (output) | Reasoning that the model writes into its visible reply reaches a client as the answer, so a client can act on a draft the model discarded; it's also stored in the vault, and wastes hours on a CPU | `medium` and `high` efforts ask for reasoning, which then comes back separately; the gateway moves reasoning written into a reply out of the answer, audits it and asks that model for reasoning from then on. Residual: the first streamed reply from a model not yet known to leak, which can't be taken back ([review](reviews/threat-model-2026-11-03.md)) | §4.1 | — |
| T-27 | Denial of service | The only copy of the backup password is on the disk that failed, so backups can't be restored | Password in the state folder by default; `password_command` for your own store. `cairnward setup`, `backup init` and `doctor` all say to keep a copy elsewhere | §4.8 | FR-SYNC-3 |
| T-28 | Elevation of privilege, info disclosure | Under WSL2 a sandboxed program reaches Windows: reads a Windows drive, starts a Windows program outside the sandbox, or opens a VM socket; or a Windows program reads Linux files through `\\wsl.localhost` | Every sandbox hides the Windows drives, `/init` and `/run/WSL`, and the doctor's self-test probes all three routes. srt blocks VM sockets, **except in the core's sandbox, which allows Unix sockets**; what they reach there is unchecked. Windows programs reading Linux files is accepted: Linux permissions don't apply, and only locking protects data. **Tested on plain Linux with stand-ins only** | Mitigated in part; unverified on WSL2 |
| T-29 | Elevation of privilege, info disclosure | A plugin, compromised or buggy, goes around a protection: sends data out itself, reads files, or uses its key elsewhere | Sandbox required, with only the gateway reachable; no route out but the fetch service, which checks, scans and audits each request; key pinned to one environment, revoked at stop, refused at the MCP endpoint; code pinned; conformance suite before it starts. Residual: redaction only catches what it recognises, so what it may legitimately send to a listed host can still carry private text |
| T-30 | Tampering (output) | A plugin's output steers the model into a risky action elsewhere | Tool calls on other servers and Cairnward's artifact tools need approval after plugin output, naming the plugin, never remembered. Residual: a model can still be misled inside the plugin's own tools, and an agent's own network requests aren't linked to the output |
| T-31 | Tampering, denial of service | The new core services are abused: oversized answers, many requests, guessing a passphrase, a redirect to this machine | Size, rate and parallel limits; `Host` set from the URL; redirects returned, so each hop is checked; addresses checked by egress, or by fetch itself without egress; wrong passphrases limited per plugin. The fetch service passes bytes through without parsing them |
| T-32 | Elevation of privilege | A compromised core asks the outer process to weaken a setting, since it can't write the configuration folder itself | The outer process only changes listed privacy and backup settings, and only if the whole configuration still loads. It can't touch allow-lists, backends or MCP servers. Changes apply on restart, and the settings diff is audited then, by a process the core doesn't control. Residual: the core can pass `confirm`, because the dashboard's own confirmation happens inside it | §5.0 | — |

## 4. LINDDUN privacy threats

| ID | LINDDUN | Threat | Controls |
|---|---|---|---|
| P-01 | Linking | Queries to academic APIs/search engines from one IP build a research profile | Network opt-in; allow-listed hosts; results cached locally for a week; optional proxy/Tor-friendly egress setting (Phase 2) |
| P-02 | Identifying | Synced chat titles/metadata reveal identity or topics on a cloud target | "Encrypt this sync target" offer; restic backups always encrypted; ☁ badge |
| P-03 | Non-repudiation | The audit log is a detailed activity record that could be used against the user | Content-free audit records (digests); retention setting; not synced unless chosen; user can export/redact |
| P-04 | Detecting | Cloud providers can infer activity from sync timing/size | On-demand mode; scheduled batching; padding not provided (documented limitation) |
| P-05 | Data disclosure | More data than needed sent to hosted backends/tools | Minimal context assembly (retrieval rather than whole documents); redaction |
| P-06 | Unawareness | User doesn't realise what left the machine | Per-chat egress report; network badge in status bar; first-time approval for each new host/target |
| P-07 | Non-compliance | Personal data of third parties (e.g. study participants in PDFs) processed or sent out without care | PII detection on egress; locked chats; deletion via crypto-shredding; export for subject-access requests |

## 5. OWASP Top 10 for LLM Applications (2025) mapping

| OWASP | Risk | Main Cairnward controls | Threats |
|---|---|---|---|
| LLM01 | Prompt Injection | Untrusted-content notes in approvals; policy engine outside the model; approvals; `user_initiated_only`. Injection test corpus in the research repository (`evals/injection`), run on the real model: one case still gets through sometimes | T-07, T-14 |
| LLM02 | Sensitive Information Disclosure | Redaction; network off by default; hosted backends denied; egress report | T-07, T-08 |
| LLM03 | Supply Chain | Pinned/hashed deps and MCP servers; licence allow-list; model format restrictions. Signed releases and SBOM: workflow built, never run on GitHub | T-04, T-15, T-16 |
| LLM04 | Data and Model Poisoning | Models only from verified sources with checksums; RAG sources marked with provenance; synced data validated | T-05, T-15 |
| LLM05 | Improper Output Handling | Model output never executed directly: diffs and commands go through approval + sandbox; artifact previews sandboxed. Open: reasoning reaching clients as the answer | T-11, T-12, T-26 |
| LLM06 | Excessive Agency | Least-privilege tool grants per environment; approvals; user-only actions | T-14 |
| LLM07 | System Prompt Leakage | No secrets in prompts (keys stay in keychain, tools hold credentials); prompts treated as non-secret | T-08 |
| LLM08 | Vector and Embedding Weaknesses | Libraries scoped to an environment; retrieved chunks carry their source, and their text is noted as untrusted in approvals. Embeddings are stored unencrypted unless the library is locked | T-07, T-24 |
| LLM09 | Misinformation | Quotation and reference checks; page-cited answers for PDFs; evals for invented citations | — |
| LLM10 | Unbounded Consumption | Budgets per client, run and environment; time limits; context warnings | T-13 |

## 6. Accepted risks (Phase 0)
- Protections the user switches off (Open preset, Custom settings) don't apply; this is the user's choice, made visible by badges and audited (spec §5.0).
- A compromised OS account or root user can read everything that's unlocked and capture passphrases as they're typed. Under WSL2 that includes any program running as the user on Windows (T-28).
- Prompt injection can't be fully prevented by any known method; Cairnward limits its impact instead (approvals, network off, user-only actions).
- Unlocked chats' search index is plaintext in SQLite; deleting it is best effort on SSDs.
- A research library is plain text unless locked, and locking doesn't cover its source PDFs (T-24).
- A recovery key opens every locked conversation it covers; it's shown once and never stored, so keeping it safe is up to the user.
- Content of third-party tools' own HTTPS traffic to allowed hosts can't be inspected (T-17).
- Copies of conversations inside clients' own databases (T-18).
- Records newer than the last audit anchor can be altered by someone with full control of the machine (T-03).
- Traffic-analysis of sync timing/size by cloud providers (P-04).
- With `home_access = "readable"`, an agent can read the home folder apart from known secret folders, and can send what it reads to any allowed host (T-12, T-17).
- With isolation off, an agent runs as the user and could answer its own approvals through the management socket; isolation off is advisory throughout (spec §5.0).
