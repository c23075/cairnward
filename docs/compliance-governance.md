# Using Cairnward in a privacy-regulated company's agent governance

Written 2026-11-06 for security, privacy and compliance teams, and for the engineers who build
in-house agents. It describes how Cairnward could serve as one layer of governance for agentic
systems, what it would contribute, and what the company would still need to supply.

This isn't legal advice, and Cairnward isn't certified against any standard. Where this page
names a regulation or standard, it's to show which kind of requirement a feature can help
evidence, not to say that using Cairnward meets it. Your privacy counsel or data protection
officer decides that.

## The short answer

Cairnward can be a **per-host enforcement and evidence layer** for agents: one instance next to
each agent workload or on each person's machine. It **can't be your whole governance layer.**
- **Designed for one user:** it has no users, roles, central policy or central log store.
- **Not production ready:** it hasn't had an independent review, and several features have only
  been tested against stand-ins.

Read [limitations.md](limitations.md) before deciding. A regulated company should adopt it only
with:
1. an engineering team that owns it, as a fork or an internal product;
2. an independent security review and penetration test;
3. a data protection impact assessment for each agent system it fronts;
4. the central pieces this page lists under "What the company must add".

## Where it sits in the stack

How Cairnward fits among the layers of an enterprise agentic system (identity, central policy,
gateways, DLP, sandboxing, SIEM, GRC) is in [overview.md](overview.md#where-it-sits-in-an-agentic-system-stack).
In short: it's the runtime enforcement and evidence layer on one host, and the central layers come
from elsewhere, as "What the company must add" lists.

## What Cairnward contributes

Each control below runs on the machine where Cairnward runs, and is switchable per environment.

| Governance need | What Cairnward does | Evidence it produces |
|---|---|---|
| **Data minimisation, and preventing disclosure** | Network off by default. Each environment has an allow-list of hosts or asks per host. Redaction of secrets and personal data in model, MCP and plugin traffic: block, ask or warn | Every connection and refusal in the audit log. Egress report per conversation (hosts, bytes) |
| **Control over third-party AI processors** | Hosted models denied, asked about or allowed per environment. Providers that may train on prompts confined to scratch environments. Locked conversations never sent out. HTTPS required | `model.call` records naming backend, environment and client. Settings snapshots show which providers were allowed when |
| **Least privilege for agents** | Agents in an OS sandbox: writes only to the workspace, the home folder hidden, network only through Cairnward. Each MCP server with a declared manifest. Tools pinned and approved per definition. Per-tool allow / ask / deny | `tool.call`, `mcp.approve`, `mcp.tool_changed`, `run.start` and `run.end` records |
| **Human oversight** | Approvals for risky actions, answered in Cairnward's own interface, never by the model. User-only actions such as encrypting or enabling hosted backends | Each question and answer, with who answered, the scope, and untrusted content that preceded it |
| **Segregation of data** | Environments per data class, client or matter, each with its own models, tools, network rules, vault retention and audit setting. The gateway refuses a run's calls outside its environment | Environment on every record |
| **Record of activity** | Hash-chained, content-free audit log of model calls, tool calls, connections, approvals, runs, settings changes, vault actions and backups. Optional signed anchors to a repository you own, and RFC 3161 timestamps | `audit verify`, `audit verify --anchors`, JSON-lines export, OpenTelemetry spans for model and tool calls |
| **Retention and erasure** | Vault retention per environment. Purging destroys the conversation's key (crypto-shredding) and can rewrite backups (`backup_purge`) | `vault.purge`, `vault.retention` and `backup.purge` records, with tombstones |
| **Access and portability** | Vault export of a conversation as text | `vault.export` records |
| **Confidentiality at rest** | Conversations encrypted per conversation. Optional passphrase locking, an optional recovery key, and encrypted backups | Lock and unlock records |
| **Change control on policy** | Every settings change is recorded with its difference, and weakening is flagged. Plugins and MCP servers pinned by hash | `settings.snapshot`, `settings.changed` |
| **Spend and runaway limits** | Budgets per run, environment and client | `429 budget_exceeded`, audited |

Examples of where these help, depending on your obligations:
- **GDPR:** data minimisation and security of processing (Art. 5(1)(c), Art. 32), records
  supporting accountability (Art. 5(2)), erasure and access requests (Art. 17, Art. 15).
- **ISO/IEC 27001 controls:** logging, access restriction and supplier relationships.
- **ISO/IEC 42001 and the EU AI Act:** human oversight and record-keeping for AI systems.

Check each against your own requirements; none is met by Cairnward alone.

## How to deploy it

Cairnward listens only on the machine it runs on, and assumes one owner. That leaves two
patterns that fit, and one that doesn't.

**Pattern A: a sidecar per agent workload.** This is the recommended one for in-house agentic
systems.
- **The host:** each agent system runs on its own host or VM, under a dedicated service account,
  with its own Cairnward instance.
- **Models:** the agent uses Cairnward's OpenAI-compatible endpoint (`/v1`) with its own client
  key, or runs under `cairnward run` to be sandboxed.
- **Tools:** come only through Cairnward's MCP endpoint, as servers declared in `mcp.toml`.
- **Internal services** the agent needs, such as a document store or a CRM, are either MCP
  servers with a permission manifest, or hosts on the environment's allow-list.
- **Larger components** can be built as a plugin under the plugin contract
  ([plugins.md](plugins.md)), running in a sandbox with no network of its own and using
  Cairnward's services.
- **Why it fits:** one instance per workload keeps "one owner" true. The audit log and vault
  belong to that workload, and the host's administrators are the owner.

**Pattern B: on each employee's workstation,** for people who run coding or research agents
themselves.
- Ship a locked baseline configuration with your device management (see below).
- Collect each machine's audit log and anchors centrally.
- The employee can still loosen settings. That's recorded, not prevented, so this pattern works
  only with monitoring of `settings.changed` and periodic checks.

**Not supported: one shared gateway for many users.** Running one instance for a team means
exposing it beyond the machine, and mixing users' data in one vault and audit log. It has no
authentication, roles or separation for that. Building it would be a new design, with its own
threat model.

## Recommended configuration for heavy compliance

Start from the Strict preset everywhere, then decide each line deliberately. Any value weaker
than Strict shows as such and is audited.

| Setting | Value | Why |
|---|---|---|
| `preset` | `strict` | All protections on |
| `isolation` | `on` | Without it, network and file rules are advisory |
| `network` | `allowlist`, with exact hosts | No asking at run time; unattended agents need a fixed list |
| `hosted_backends` | `deny`, or `ask` for an environment cleared by your vendor review | Hosted providers see prompts in plain text |
| `training_backends` | `scratch_only` | Providers that may train on prompts never see regulated data |
| `redaction` | `enforce` | Secrets blocked, personal data needs approval. Install Presidio and set `[redaction] presidio = true` |
| `[redaction] custom` | Patterns for your own identifiers: client codes, matter numbers, internal IDs | Built-in patterns don't know them |
| `vault` | `full`, or `digests` where you mustn't keep content | `digests` keeps only the audit trail |
| `crypto_shred` | `on` | Makes erasure reliable. With `off`, content is stored as plain text |
| `vault_retention_days` | Your retention schedule, per environment | Purges automatically, and records it |
| `backup_purge` | `on` | Purges also reach backups |
| `audit` | `on` | |
| `audit_anchor` | `git`, with `[anchor] repo`, and `tsa_url` | Proof that holds even if the host is compromised, and trusted timestamps |
| `mcp_pinning` | `on` | Changed tool definitions are disabled until reviewed |
| `home_access` | `hidden` | |
| `egress_report` | `on` | |
| `tls_inspection` | Only where method and path rules are needed, and with the CA key managed as a secret | Adds visibility, but also risk (T-33) |
| `[run_budget]`, client budgets | Set | Stops runaway agents |

Also:
- **Use one environment per data classification,** or per client or matter. Give each its own
  allow-list, MCP servers, retention and backends. Don't share an environment across matters
  that need an ethical wall.
- **Turn on full-disk encryption on every host.** Unlocked conversations' keys are on the same
  disk as the vault. Lock conversations that hold special-category data.
- **Keep the backup password, the anchor signing key and any recovery key** in your secrets
  manager, not on the host.
- **Point agents at local models** for anything regulated. A hosted model is a processor
  relationship that needs its own contract and assessment.

## What the company must add

| Gap | Compensating control |
|---|---|
| **No identity or roles.** Approvals and settings belong to the host's owner | Pattern A: restrict host access to named administrators with your identity provider and privileged-access tooling. Approvals are then made by those administrators, with the OS account as the identity |
| **No central policy.** Configuration is local files the owner can change | Deliver the config folder with configuration management, compare it with a signed baseline, and alert on drift. Alert on every `settings.changed` and `audit.disabled` record |
| **No central audit store** | Ship `audit.jsonl` to your SIEM with a log agent. Keep each host's anchors repository centrally and run `audit verify --anchors` on a schedule. The OpenTelemetry export can feed a collector on the same host that forwards elsewhere; it covers model and tool calls only |
| **No audit retention or legal hold** | Retain and expire records in your SIEM under your schedule. Vault retention purges conversations, so suspend `vault_retention_days` for environments under legal hold |
| **Redaction is incomplete,** text only, and recognises little that's specific to you | Custom patterns, Presidio, `enforce` mode, local models for regulated data, and your own DLP on the hosts |
| **Attachments aren't scanned** | Don't pass images or files to hosted models from regulated environments |
| **Allowed hosts can receive anything** | Keep allow-lists to the hosts a workload truly needs. Use TLS inspection with method and path rules where an allowed host offers dangerous actions. Watch egress volumes |
| **Unproven platforms and features** | Run only on Linux hosts. Treat stand-in-tested features (hosted providers, remote MCP, OpenShell, TLS inspection) as pilots until tested in your environment |
| **No independent review** | Commission a security review and penetration test of your build. Repeat the threat model ([threat-model.md](threat-model.md)) for your deployment |
| **Maintenance** | Pin a reviewed version. Track dependencies (they're hash-locked). Own security fixes, because this is a one-person project |

## Data flows for an impact assessment

These are the flows a data protection impact assessment would need to describe for each agent
system behind Cairnward. The step numbers in the table match the list below.

1. User or system input reaches the agent.
2. The agent calls the model through Cairnward's gateway. Redaction applies; the model may be
   local or hosted.
3. The exchange is stored in the vault, encrypted, unless `vault` is `digests` or `off`.
4. The agent calls tools through Cairnward's MCP endpoint. Tool servers may reach their declared
   hosts through egress.
5. The agent's other network traffic goes through egress, to allowed hosts only.
6. Audit records of every step stay on the host, and are shipped to your SIEM.
7. Backups are encrypted with restic and stored in your repository. If set, replication goes
   through an encrypted shared folder.

| Step | Where the data is | In plain text to |
|---|---|---|
| 2, local model | The host | The model server on the host |
| 2, hosted model | The provider | The provider, under its terms |
| 3 | `vault.db` | Anyone with the disk, for unlocked conversations, unless the disk is encrypted |
| 4 | Tool servers | The tool server, and the hosts it reaches |
| 5 | Allowed hosts | Those hosts |
| 6 | Audit log, SIEM | No content: metadata and digests |
| 7 | Backup repository | Nobody without the backup password |

## Adopting it in stages
1. **Evaluate.** Read [limitations.md](limitations.md), [threat-model.md](threat-model.md) and
   [status.md](status.md). Decide whether to own a fork.
2. **Pilot one workload** with Pattern A, on non-regulated data:
   - set up the configuration above, anchoring and SIEM shipping;
   - run the agent's real tasks, and tune allow-lists and tool approvals;
   - measure how often approvals and redaction interrupt it.
3. **Harden:**
   - an independent review and penetration test;
   - a restore test of the backups;
   - an incident-response runbook that uses the audit log and anchors;
   - a data protection impact assessment.
4. **Limited production** on regulated data, with drift monitoring and scheduled
   `audit verify --anchors`.
5. **Scale** workload by workload. Revisit whether the gaps above justify building central policy
   and audit services around Cairnward, or adopting a commercial gateway that has them
   ([landscape.md](landscape.md)).

## Planned: enterprise proof of concept and model risk management

A plan to extend Cairnward into an enterprise proof of concept, with LLMOps and model risk
management (inventory, validation, monitoring), is in
[plan/enterprise-poc.md](plan/enterprise-poc.md). Nothing in it is built yet.
