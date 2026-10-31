# ADR-0001: Record architecture decisions

- Status: Accepted
- Date: 2026-10-31
- Spec: §2, D32

## Context
The spec grew through several interview rounds; decisions were recorded as one-line rows in a table. That captures *what* was decided but not *why*, which options were rejected, or what the decision costs. Future contributors (and future us) need that to change decisions safely.

## Options
1. Keep the decisions table only: cheap, but loses rationale.
2. Architecture Decision Records (Michael Nygard's format, widely used): one short file per decision.

## Decision
Use ADRs in `docs/adr/` for significant decisions. Keep the spec's decisions table as the index, linking to ADRs where they exist. Small decisions can stay as table rows.

## Consequences
- Each significant change of direction needs a short written record.
- Reviews can check a PR against the relevant ADRs.
