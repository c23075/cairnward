# Architecture Decision Records

One short file per significant decision, in the format: context, options, decision, consequences. Status is one of *Proposed*, *Accepted*, *Superseded by ADR-NNNN*.

ADRs are never edited after acceptance except to change their status; a changed decision gets a new ADR that supersedes the old one. The decisions table in [spec §2](../spec.md#2-decisions-log-from-interview) is the index.

| ADR | Title | Status |
|---|---|---|
| [0001](0001-record-architecture-decisions.md) | Record architecture decisions | Accepted |
| [0002](0002-hybrid-build.md) | Hybrid build: own core, reuse open-source parts | Superseded by 0009 |
| [0003](0003-python-now-rust-later.md) | Python now, Rust later for audit and egress | Accepted |
| [0004](0004-pluggable-backends.md) | Pluggable model backends via OpenAI-compatible and Ollama drivers | Accepted |
| [0005](0005-local-core-service-and-api-hardening.md) | Local core service with thin clients; local API hardening | Accepted (client part superseded by 0009) |
| [0006](0006-encryption-age-key-wrapping-crypto-shredding.md) | Encryption with age, key wrapping and crypto-shredding | Accepted |
| [0007](0007-sync-restic-backup-and-event-log-replication.md) | Sync: restic for backup, per-device event logs for replication | Accepted (amended: backup purge) |
| [0008](0008-hash-chained-audit-log.md) | Hash-chained audit log, OpenTelemetry export, Merkle later | Accepted (amended: git anchoring) |
| [0009](0009-gateway-pivot.md) | Pivot to a client-neutral privacy gateway plus research workflows | Accepted |
| [0010](0010-os-level-egress-enforcement.md) | Enforce egress at the OS level | Accepted |
| [0011](0011-transcript-vault.md) | Transcript vault for conversations from any client | Accepted |

Template: copy [`template.md`](template.md).
