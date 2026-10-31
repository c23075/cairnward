# Cairn threat model

Status: Draft v0.1 (goes with spec v0.5)
Method: **STRIDE** (security) and **LINDDUN** (privacy) applied to the components and data flows in [spec §3](spec.md#3-architecture), with every control mapped to the **OWASP Top 10 for LLM Applications (2025)**. Each threat has an ID; controls point to spec sections and requirement IDs in [requirements.md](requirements.md).

Review this document whenever a component, data flow or trust boundary changes, and at least once per phase.

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
| B4 | Core ↔ sandbox / MCP servers | Core | Commands, scripts, third-party MCP servers |
| B5 | Core ↔ network (egress proxy) | Core | Internet hosts, hosted model backends, sync targets |
| B6 | Core ↔ local disk | Core while running | Anyone with later access to the disk or a backup |

The model is **not** a trusted component: anything it decides passes through the policy engine (B2).

## 3. STRIDE threats

| ID | STRIDE | Threat | Controls | Spec | Verified by |
|---|---|---|---|---|---|
| T-01 | Spoofing | A malicious web page sends requests to the local API (CSRF / DNS rebinding) | Unix socket default; Host allowlist; Origin check; SameSite cookie + CSRF token; no CORS | §3.1 | NFR-S3, FR-API-3 |
| T-02 | Spoofing | Another local user or process uses the API | Socket/token file mode 0600 (per-user ACL on Windows); single-use login link | §3.1 | FR-API-2 |
| T-03 | Tampering | Audit records edited or deleted after the fact | Hash chain; signed checkpoints; exportable checkpoints kept elsewhere; `audit verify` | §5.2 | FR-AUD-2 |
| T-04 | Tampering | MCP server changes its tool descriptions after approval (rug pull) or ships hidden instructions (tool poisoning) | Version + hash pinning; tool-definition hashes; disable on change; description scan; sandboxed servers | §4.9 | FR-TOOL-4 |
| T-05 | Tampering | Synced events from another device are malformed or malicious | Schema validation; events from other devices are data, never instructions; signed per-device logs (Phase 1) | §4.12.3 | FR-SYNC-5 |
| T-06 | Repudiation | Unclear whether the user or the agent caused an action | `actor` field + approving user action recorded on every effect; sub-agent nesting | §5.2 | FR-AUD-1 |
| T-07 | Info disclosure | Prompt injection makes the agent send data to an attacker (e.g. URL with data in the query) | Network off by default; host allowlist; redaction; approval of outbound requests showing the payload; untrusted-content tagging | §5.3–5.4 | NFR-S1, FR-NET-2 |
| T-08 | Info disclosure | Secrets or personal data sent to a hosted model or search engine | Redaction (secrets block, PII ask); hosted backends denied by default; locked chats never sent to hosted backends | §4.1, §5.4 | FR-RED-1, FR-BACK-4 |
| T-09 | Info disclosure | Disk, laptop or backup stolen | Opt-in chat/artifact locking with age; restic backups always encrypted; full-disk encryption recommended at first run | §5.5 | FR-ENC-1 |
| T-10 | Info disclosure | "Deleted" chats recovered from SSD remnants or old copies | Crypto-shredding (destroy per-chat key); `secure_delete` + VACUUM for the plaintext index (best effort, stated) | §4.5, §5.5 | FR-ENC-5 |
| T-11 | Info disclosure | Artifact preview (HTML/SVG) runs scripts that read Cairn data or phone home | Sandboxed iframe on a separate origin; CSP with no network | §4.8 | FR-ART-4 |
| T-12 | Info disclosure | Sandbox escape or command reads files outside the workspace | Tier 1/2 sandboxes; tier 4 off by default and approval per command; read-only mounts | §4.10.1 | FR-CODE-3 |
| T-13 | Denial of service | Runaway agent loop, sub-agent explosion or huge prompt locks the machine | Effort budgets (tool steps, tokens, time); sub-agent depth/parallel limits; context-size warnings; cancel at any time | §4.2, §4.14 | FR-AGENT-3 |
| T-14 | Elevation of privilege | Injected content triggers user-only actions (encrypt everything, add sync target, enable hosted backend) | `user_initiated_only` actions | §5.8 | FR-POL-3 |
| T-15 | Elevation of privilege | Malicious model file runs code on load | GGUF/safetensors only; checksum verification | §5.7 | FR-SUP-3 |
| T-16 | Elevation of privilege | Compromised dependency or release | Hash-locked deps; pip-audit; signed releases + SBOM; pinned CI actions | §5.7 | NFR-SUP-1 |

## 4. LINDDUN privacy threats

| ID | LINDDUN | Threat | Controls |
|---|---|---|---|
| P-01 | Linking | Queries to academic APIs/search engines from one IP build a research profile | Network opt-in; per-query approval option; local caching of results; optional proxy/Tor-friendly egress setting (Phase 2) |
| P-02 | Identifying | Synced chat titles/metadata reveal identity or topics on a cloud target | "Encrypt this sync target" offer; restic backups always encrypted; ☁ badge |
| P-03 | Non-repudiation | The audit log is a detailed activity record that could be used against the user | Content-free audit records (digests); retention setting; not synced unless chosen; user can export/redact |
| P-04 | Detecting | Cloud providers can infer activity from sync timing/size | On-demand mode; scheduled batching; padding not provided (documented limitation) |
| P-05 | Data disclosure | More data than needed sent to hosted backends/tools | Minimal context assembly (retrieval rather than whole documents); redaction; payload preview on approval |
| P-06 | Unawareness | User doesn't realise what left the machine | Per-chat egress report; network badge in status bar; first-time approval for each new host/target |
| P-07 | Non-compliance | Personal data of third parties (e.g. study participants in PDFs) processed or sent out without care | PII detection on egress; locked chats; deletion via crypto-shredding; export for subject-access requests |

## 5. OWASP Top 10 for LLM Applications (2025) mapping

| OWASP | Risk | Main Cairn controls | Threats |
|---|---|---|---|
| LLM01 | Prompt Injection | Untrusted-content tagging; policy engine outside the model; approvals; `user_initiated_only`; injection test corpus | T-07, T-14 |
| LLM02 | Sensitive Information Disclosure | Redaction; network off by default; hosted backends denied; egress report | T-07, T-08 |
| LLM03 | Supply Chain | Pinned/hashed deps and MCP servers; signed releases; SBOM; model format restrictions | T-04, T-15, T-16 |
| LLM04 | Data and Model Poisoning | Models only from verified sources with checksums; RAG sources marked with provenance; synced data validated | T-05, T-15 |
| LLM05 | Improper Output Handling | Model output never executed directly: diffs and commands go through approval + sandbox; artifact previews sandboxed; markdown rendered without raw HTML in the chat view | T-11, T-12 |
| LLM06 | Excessive Agency | Least-privilege tool grants per environment; approvals; user-only actions; sub-agents get a subset of permissions | T-14 |
| LLM07 | System Prompt Leakage | No secrets in prompts (keys stay in keychain, tools hold credentials); prompts treated as non-secret | T-08 |
| LLM08 | Vector and Embedding Weaknesses | Per-chat/folder index scoping; locked chats' embeddings encrypted; retrieved chunks tagged with source and trust level | T-07, T-09 |
| LLM09 | Misinformation | `citation-check` (every reference must resolve); page-cited answers for PDFs; evals for invented citations | — |
| LLM10 | Unbounded Consumption | Effort budgets; time limits; sub-agent limits; context warnings | T-13 |

## 6. Accepted risks (v1)
- A compromised OS account or root user can read everything that's unlocked and capture passphrases as they're typed.
- Prompt injection can't be fully prevented by any known method; Cairn limits its impact instead (approvals, network off, user-only actions).
- Unlocked chats' search index and embeddings are plaintext in SQLite; deleting them is best effort on SSDs.
- Tier-4 (unsandboxed) runs, when the user enables them, can read the user's files and use the network.
- Traffic-analysis of sync timing/size by cloud providers (P-04).
