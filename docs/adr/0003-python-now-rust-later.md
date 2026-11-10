# ADR-0003: Python now, Rust later for audit and egress

- Status: Accepted
- Date: 2026-10-31
- Spec: §6, D9

## Context
The best libraries for retrieval, PDF parsing, academic APIs and MCP are Python. A Rust core would be smaller, memory-safe and easier to audit, but slower to build and would need Python helper processes anyway. The model, not the agent code, is the speed bottleneck.

## Options
1. Python throughout.
2. Rust core + Python tool helpers.
3. Python now; move the most security-critical, stable parts (audit log, egress proxy) to Rust once their interfaces settle.

## Decision
Option 3. Keep the audit log and egress proxy behind narrow interfaces (`AuditSink`, `EgressProxy`) so they can be replaced by Rust implementations (via PyO3 or as separate processes) without changing callers.

## Consequences
- Fast iteration in Phase 0.
- Must keep core dependencies minimal and hash-locked (Python's dependency tree is the main audit cost).
- Rust rewrite is planned in Phase 3, not committed to a date. ("Phase 3" is now "Later" in [spec §9](../spec.md#9-delivery-plan-summary), with no date.)
