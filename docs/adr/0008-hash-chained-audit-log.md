# ADR-0008: Hash-chained audit log, OpenTelemetry export, Merkle later

- Status: Accepted; amended 2026-10-31 by spec D49: checkpoints are anchored to a private git repo owned by the user
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
