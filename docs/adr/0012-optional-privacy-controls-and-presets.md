# ADR-0012: Optional privacy controls with presets and per-environment overrides

- Status: Accepted
- Date: 2026-10-31
- Spec: §5.0, D55–D58

## Context
Spec v0.6 made most protections mandatory (OS isolation, network off, approvals, redaction, vault, audit). The user wants every privacy option to be optional: some work needs no protection (scratch experiments, public code), some needs the strictest setting (confidential research), and friction on low-risk work pushes people to abandon the tool.

## Options
1. Mandatory protections (v0.6): strongest guarantees; friction everywhere.
2. Everything optional, privacy-first defaults: user switches things off.
3. Everything optional, **preset chosen at first run** (Open / Balanced / Strict), global + per-environment overrides.
4. Also per-client overrides: more flexible; harder to reason about.

## Decision
Option 3. All protections are settings in a `[privacy]` table, resolved environment → global. Presets set them all; individual changes mark the preset *Custom*. Virtual models carry the environment, so model choice in any client also selects the privacy level. Weakening is allowed without confirmation but is always visible (badges, `cairnward privacy show`) and logged (`settings.changed`; `audit.disabled`/`audit.enabled` around audit gaps).

Kept non-switchable: the passphrase dialog for locking (functional) and the dashboard/endpoint's local-only binding and Host/Origin checks (switching these off exposes Cairnward rather than reducing friction). Open question in spec §12.

## Consequences
- Each protection must be implemented as an independent, testable component that can be disabled without breaking others; tests run the full suite under all three presets plus "everything off".
- Guarantees in the threat model hold only for the protections that are on; the threat model states its assumed preset.
- Some combinations are ineffective (e.g. allowlist without isolation is advisory); the UI must explain them.
- The first-run setup and `cairnward doctor` become part of M0.
