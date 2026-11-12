# ADR-0021: Managed mode, an opt-in link to an enterprise control plane (Cairnsteward)

- Status: Proposed 2026-11-11, revised 2026-11-12, for the owner's review with Cairnsteward's spec v0.4
- Date: 2026-11-11
- Spec: §4.1, §5.0, §5.2, §5.3, D55, D79, D82; the [enterprise plan](../plan/enterprise-poc.md), item 1

## Context
The enterprise plan's first item is an ADR for the control-plane split: which interfaces the
control plane may use, its trust boundary with the sidecars, and how validation status reaches
the policy engine. The control plane is now specified as
[Cairnsteward](https://github.com/c23075/cairnsteward) (spec v0.3, proposed): users and roles,
model risk management, signed receipts for audit records, and evidence for GRC tools. Its
decisions are recorded there (K13–K37, ADRs 0005–0013); this ADR records what they ask of
Cairnward.

Cairnward is a personal privacy gateway first, and must stay one for anyone who doesn't enrol.

## Options
1. **No Cairnward changes:** the control plane writes TOML files through configuration management
   and reads D82's endpoints. Pros: the core doesn't change. Cons: no enforcement of validation
   status at call time (the plan's item 4), no per-user attribution, no receipts on the host.
2. **An opt-in managed mode in Cairnward** that pulls signed bundles (environments and permission
   profiles), ships its records, stores receipts and calls a private model server with
   Steward-signed tokens; plus a `model_validation` policy setting. Pros: covers
   the plan; no inbound control channel; personal mode unchanged. Cons: new code in the outer
   process and the policy engine, a new configuration layer.
3. **A live control API** for the control plane through the management socket. Pros: immediate.
   Cons: the control plane could weaken every host at once.

## Decision
Option 2, all of it inert until `cairnward steward enroll`:
- **Interfaces the control plane may use:** nothing inbound. Cairnward's outer process dials out
  over mutual TLS to pull signed bundles and ship audit records (the same records D82 exposes).
  No live commands, ever; approvals stay on the host.
- **Trust boundary:** Cairnward trusts a bundle only if its Ed25519 signature checks against the
  key pinned at enrolment, its instance ID is this one, and its version is not lower than the last
  applied. Bundles that loosen anything are signed only with two admins' approval, by a separate
  signer (Cairnsteward's K32). Every applied bundle is audited (`steward.bundle_applied`) with its
  digest and the settings diff.
- **Environments and permission profiles:** in managed mode, the bundle defines the environments;
  the host owner may tighten any of them, never loosen. Each user may use only the environments in
  their profiles; Cairnward checks this on every model call and `cairnward run` and refuses with
  the reason `profile` (closing I39 for managed mode).
- **Approvals stay local:** the bundle carries approval policy per environment; the person at the
  instance answers, as today. On an unattended workload sidecar, `ask` becomes `deny` unless an
  approver is named. The control plane only audits approval records.
- **Users:** a bundle's users arrive as hashed keys and act as clients named `user:<id>`, so audit
  records carry the user. On a workload sidecar, each conversation's key (ADR-0006) is wrapped to
  its user's age public key from the bundle, so the sidecar writes transcripts it can't read back,
  and only that user can export them.
- **Models:** by default a managed instance calls the company's private model server through a
  gateway, sending a short-lived model-access token (received with each bundle poll) and the
  user's ID. Revocation is enforced at the gateway. Local models and third-party providers are
  used only where a profile allows them.
- **Validation status reaches the policy engine** through the bundle's inventory entries: a new
  environment setting `model_validation = off | warn | ask | enforce` (switchable, D55; `off`
  unless set) checks each model call's version `(backend, model, digest)`. A version not approved
  for that environment, or with no inventory record, is warned about, asked about or refused, and audited with the reason
  `validation`. The `validation` environment is exempt, so versions can be evaluated before
  approval. Cairnward reports each served model's digest.
- **Receipts:** signed receipts over the RFC 9162 Merkle root (D79) are stored beside anchors;
  `cairnward audit verify --receipts` checks them.
- **Leaving:** `cairnward steward leave` removes the bundle's effects, is audited, and is shipped
  to the control plane before disconnecting.

## Consequences
- **Personal mode must not change:** CI runs the full suite with no enrolment, and benchmarks
  check that personal mode adds no overhead.
- New code: a managed-mode client in the outer process, environments and profiles from bundles,
  a per-key environment check, approval policy from bundles, per-user key wrapping in the vault,
  model-access tokens on backend calls, the `model_validation` check, digest reporting, receipt
  storage and verification. Each needs a
  threat-model review here when built.
- A compromised control-plane signer, or two colluding admins, can loosen managed hosts within
  managed mode (Cairnsteward's T-17). The host owner can always tighten, or leave.
- Nothing is built. The phases are in Cairnsteward's `docs/plan/enterprise.md` (E2 and E3 touch
  Cairnward).
