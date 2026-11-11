# Plan: an enterprise proof of concept, LLMOps and model risk management

Written 2026-11-09 (moved here from compliance-governance.md). A plan. **One piece is built:** the fleet view, as [Cairnward Keep](https://github.com/c23075/cairnward-keep), a separate observe-only control plane (2026-11-11, D82). The rest isn't built yet. The owner intends
to extend Cairnward into a proof of concept of an enterprise agent-governance stack, as an LLMOps
portfolio project, with model risk management (MRM) and model validation as a focus.

Frameworks that set MRM expectations include the Federal Reserve and OCC's SR 11-7 (US), the
PRA's SS1/23 (UK, which covers AI and machine-learning models explicitly), and, for high-risk AI
systems, the EU AI Act's risk-management and record-keeping duties. As elsewhere on this page,
naming them shows what kind of requirement a feature could help evidence; Cairnward meets none of
them, and how they'd apply hasn't been assessed.

## What Cairnward already demonstrates

It's a working proof of concept for **one layer**: enforcement and evidence on a single host,
as a sidecar next to one agent workload (Pattern A). These work end to end and are covered by
tests, some only against stand-ins (see [status.md](../status.md)):
- **Model gateway:** policy and budgets; hosted providers allowed, asked about or denied per
  environment.
- **Sandboxed agent runs:** egress is the only way out.
- **Tool governance:** MCP tools pinned by hash and approved per definition.
- **Approvals:** answered by a person in Cairnward's interface, never by the model.
- **Redaction** of text going to models, MCP servers and plugins.
- **Audit log:** hash-chained, digests rather than content, with anchors and OpenTelemetry
  export.
- **Vault:** encrypted per conversation, with crypto-shredding on purge.

Because it sits on every model call, it already collects much of the evidence MRM needs about how
models are *used*. What it lacks is the validation side, judging whether a model is fit for its
purpose, and the central pieces an enterprise needs (see [what the company must add](../compliance-governance.md#what-the-company-must-add)).

What exists for deployment today: a systemd unit (`deploy/systemd/`) and container setups for
two chat clients (`deploy/containers/`). There's no container image or compose file for a
Cairnward sidecar yet.

## Platform pieces a proof of concept would add

Each would be the smallest thing that demonstrates the pattern:

| Missing piece | Smallest thing that would demonstrate it |
|---|---|
| **Central policy** | A Git repository of signed baseline configurations pushed to each sidecar, with a drift check that compares the running configuration to the baseline and raises an alert |
| **Identity** | Each sidecar's client keys tied to a service account in an identity provider (for example OIDC workload identity), and approvers identified through SSO instead of "the host's owner" |
| **Central audit** | A log shipper (Vector or Fluent Bit) sending `audit.jsonl` to a SIEM or OpenSearch, and a scheduled central `audit verify --anchors` across all hosts |
| **Central approvals** | An inbox that gathers pending requests from several sidecars, with a named approver for each. The largest new piece |
| **Fleet view** | A dashboard listing the sidecars: version, privacy level, weakened settings, recent denials. **Built** as [Cairnward Keep](https://github.com/c23075/cairnward-keep): an agent per host reads the management socket (read-only, D82) and reports over mutual TLS; the server shows every session and checks each audit chain off the host |
| **Reference workloads** | Two or three containerised agents (a coding agent, a retrieval agent over an internal document store, an agent using a CRM tool), each with its own sidecar and data class |

## Model risk management: today and the gaps

| MRM requirement | What Cairnward has today | Gap |
|---|---|---|
| **Model inventory** | Every model and backend is declared in `backends.toml`; virtual models (`cw/<environment>:<effort>`) tie each one to an environment. Model files are downloaded hash-pinned (`cairnward models pull --sha256`) | No inventory record per model with an owner, intended use, risk tier, validation status or approval date |
| **Use restrictions** | Each environment decides which backends, models and tools may be used for which data class. Providers that may train on prompts are limited to scratch environments. A run's key can't call models outside its environment | No link between "validated for use X" and the environments where the model may run |
| **Change control** | Settings changes are audited with their difference and flagged when weaker. Tool definitions and plugins are pinned by hash | A new model version or new weights isn't treated as a model change needing revalidation |
| **Ongoing monitoring** | Each call is audited with backend, model, tokens, duration, fallbacks and denials. OpenTelemetry export; benchmarks in [bench/](../bench/) | No quality metrics (drift, accuracy, hallucination or refusal rates), thresholds or alerts |
| **Validation and testing** | `evals/coding/run_eval.py` runs an agent through `cairnward run` and reports pass/fail, time, model calls and tokens from the audit log | One small coding eval. No validation suite (accuracy, robustness, bias, prompt-injection resistance), no challenger model, no validation reports |
| **Effective challenge and independence** | Approvals are separated from the model. The audit chain can be checked independently against anchors | No validator role or sign-off workflow; Cairnward has no identities |
| **Documentation** | Spec, ADRs, threat model and review records | No model documentation template (a model card: purpose, limits, data, validation results) |

## Design rule: a control plane around the sidecars, not inside them

The new pieces would be a separate control-plane project that talks to the sidecars only through
interfaces they already have: configuration files, the audit export, OpenTelemetry and the
control socket. The core barely changes, so its threat model ([threat-model.md](../threat-model.md))
still holds; the control plane would need its own. The one exception is item 4 below: the
policy engine would read each model's validation status, so a call can be refused at the moment
it's made.

**Not planned:** turning Cairnward into one shared multi-user gateway. It has no
authentication, roles or tenant separation, and adding them would be a new design (see "Not
supported" under [how to deploy it](../compliance-governance.md#how-to-deploy-it)). A fleet of sidecars with a thin control plane demonstrates
the enterprise pattern without that.

## Planned work, in order
1. **An ADR** for the control-plane split: which interfaces it may use, its trust boundary with
   the sidecars, and how validation status reaches the policy engine.
2. **A sidecar container image** and a compose file with two sidecars, each in front of a
   reference agent.
3. **Central audit and drift:** audit shipping to OpenSearch, a scheduled anchor check, and a
   drift alert against a signed baseline configuration.
4. **Model inventory enforced by policy:** a record per model (owner, purpose, risk tier,
   validation status and expiry); an environment refuses, or asks about, a model whose
   validation has expired or doesn't cover its use. This fits the existing allow / ask / deny
   design, and the refusal is audited like any other. Few gateways enforce validation status at
   call time, so this is the most distinctive item.
5. **Validation harness:** extend `evals/` into a suite that runs through the gateway, so every
   validation run is itself audited: task accuracy, robustness, refusal behaviour,
   prompt-injection resistance, and comparison with a challenger model. Each run produces a
   validation report stored with the model's inventory record.
6. **Ongoing monitoring:** quality and stability metrics computed from the audit log and
   OpenTelemetry (latency, fallback rate, refusal rate, eval scores over time), with thresholds
   that raise alerts, shown in the fleet view.
7. **Revalidation on change:** a new model version or new weights (a different hash) marks the
   model "needs revalidation" until it's approved again.
8. **Identity, then central approvals:** an identity provider in the compose file, approvers and
   validators signing in through SSO, and last, the central approvals inbox with a sign-off step
   for validation reports.

## Caveats
- **Non-regulated data only.** Cairnward has had no independent review, and hosted providers,
  remote MCP servers, OpenShell and TLS inspection have only been tested against stand-ins.
- **It demonstrates an architecture, not a product.** Compare it with a commercial gateway that
  has central policy and audit ([landscape.md](../landscape.md)).
- **Evidence, not compliance.** The MRM pieces would produce evidence a validation team could
  use; they wouldn't make anyone compliant with SR 11-7, SS1/23 or the AI Act.
- **Independence.** MRM expects validators to be independent of developers. That needs identity
  (item 8); until then, sign-off is by the host's owner.
- **Validation metrics need real data.** Evals on synthetic tasks show the harness works, not
  that a model is fit for a real use.
