# ADR-0008: Hash-chained audit log, OpenTelemetry export, Merkle later

- Status: Accepted; amended 2026-10-31 by spec D49: checkpoints are anchored to a private git repo owned by the user; amended 2026-11-10 by D79: Merkle checkpoints built
- Date: 2026-10-31
- Spec: §5.2, D8, D37

## Context
The audit log must reveal tampering, be verifiable without passphrases, avoid storing content (it's sensitive itself), and ideally interoperate with standard tooling.

## Options
1. Hash chain (each record includes the previous record's hash) + signed checkpoints: simple; verifying one record needs the chain.
2. Merkle tree (Certificate Transparency / Sigstore Rekor style): efficient inclusion and consistency proofs; more complex.
3. OpenTelemetry traces only: standard tooling; not tamper-evident.

## Decision
Option 1 for Phase 0, with Ed25519-signed, exportable checkpoints. Optional OpenTelemetry export using the GenAI semantic conventions for observability (off by default, local only unless the user opts in). Merkle checkpoints in Phase 3. ("Phase 3" is now "Later" in [spec §9](../spec.md#9-delivery-plan-summary), with no date.)

## Consequences
- `cairn audit verify` is O(n) over the log; fine at single-user scale.
- Records hold digests and metadata, so locked chats stay private in the log.
- OTel export gives Grafana/Jaeger views without making them the source of truth.

## Amendment 2026-11-10 (D79): Merkle checkpoints
Option 2 was added on top of option 1 rather than replacing it. The hash chain stays the record
format; each anchored checkpoint now also signs the RFC 9162 Merkle root over every record up to
it (format `"v": 2`; version 1 checkpoints still verify).
- **Why now:** it lets one record be shown and checked against a signed checkpoint without the
  rest of the log (`audit prove`, `audit check-proof`), which an outside auditor or a model risk
  review needs, and it checks that each checkpoint extends the one before.
- **Why RFC 9162:** a published, widely implemented construction (Certificate Transparency),
  checked here against its reference roots, rather than a tree of our own.
- **What it doesn't change:** records newer than the last checkpoint can still be altered by
  someone controlling the machine (T-03). Verification of the whole log stays O(n).
