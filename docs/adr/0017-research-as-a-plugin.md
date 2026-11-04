# ADR-0017: Research as a separate plugin, under a standard plugin contract

- Status: Accepted (2026-11-05)
- Date: 2026-11-04
- Spec: §1, §3.2, §4.2, §4.6, §5.3, §5.4; D15, D42, D43, D55, D63, D67, D71; FR-RES-1 to
  FR-RES-13, FR-RED-2
- Amends: [ADR-0009](0009-gateway-pivot.md) (product direction: gateway *plus* research).
  [ADR-0014](0014-literature-engine.md) is unchanged and moves with the research package.

## Context
D42 made Cairnward "a privacy gateway plus its own literature-review workflows", and D43 treats
coding and research as equally important. Coding is done by existing agents that run through
the gateway. Research was built into Cairnward itself. On `main` as of 2026-11-04 (a57444e):

- `src/cairnward/research/` is about 5,200 lines (22% of the source), and its tests are about
  2,500 lines (23% of the tests).
- The research code hardly depends on the core. The coupling runs the other way: the core
  reaches into research in about a dozen places.

| Where | What it does with research |
|---|---|
| `core/server.py` | Runs research inside the core process: in-process chat and embeddings, parsing hooks, the job runner, PaperQA2 and Docling executors |
| `core/management.py` | Endpoints for ask, online search and fetch, and jobs |
| `egress/control.py` | Runs the PaperQA2 and Docling workers, and backups, for the isolated core (which can't start sandboxes) |
| `mcp/hub.py` | Built-in `lit_search`, `lit_ask`, `lit_find`, `lit_review_start` |
| `policy/engine.py` | Hard-coded rules for those built-in tools |
| `cli.py` | The `library`, `research`, `lit` and `cite` commands (about 120 references) |
| `config/models.py` | `ResearchConfig`, `research_sources`, `tools = ["research", "scholar"]`, backup's `include_research` |
| `store/migrations/research/` | The research database |
| `redaction.py` | Imports Presidio from `research/engines/presidio.py` |
| Smaller | Dashboard jobs view, `doctor`, `check`, a PaperQA2 cache path in `run/runner.py` |

What this costs:

1. **Security surface.** The egress process is meant to be the one process with internet
   access, with a small job. It now also launches research workers. The isolated core parses
   PDFs, which are untrusted input.
2. **Scope.** Research is a consumer of the gateway, not part of it. A protection change and a
   research change land in the same release, are tested in the same suite, and are held to the
   same review standard.
3. **Evidence.** Every research result so far comes from one machine (Ryzen 3 3200G, CPU only),
   one model pair (`qwen3:30b` and `qwen3:1.7b`) and synthetic papers. That limits what can be
   claimed for research, but the gateway's own claims don't depend on it.

Moving research out raises a second question: how should Cairnward run a plugin safely at all?
Today an MCP server's protections are a mix. Some are enforced by the operating system or the
core: the sandbox, the egress allow-list, tool pinning, per-tool policy. Others depend on the
server behaving well. A sandbox controls what a plugin can touch. It can't control what the
plugin sends to a host it is allowed to reach, what its output does to the model that reads it,
or whether that output is correct.

## Options
1. **Leave it as it is.** No work and nothing to retest. The coupling and the attack surface
   stay, and every research feature keeps growing the core.
2. **A Python plugin loaded into the core** (a `cairnward.plugins` entry point that adds CLI
   commands, management routes, built-in MCP tools and a config section). Mostly moving files.
   The research code still runs in the core's process with the core's rights, so the core is
   only tidier, not smaller in security terms. It also needs a stable in-process plugin API that
   the core then has to keep.
3. **A separate package run as a sandboxed MCP server, with the plugin's own network access**
   (manifest lists the academic API hosts), plus an outbound-check endpoint the plugin should
   call before sending text out. Smaller core. But redaction of outgoing text becomes
   voluntary: a buggy or compromised plugin can skip it, and egress can't see inside HTTPS.
4. **A separate package run as a sandboxed MCP server under a plugin contract with brokered
   services.** As option 3, but the plugin has no network of its own. Everything it needs
   from outside its sandbox (models, outside requests, durable output) goes through a core
   service that enforces the rules on every request. No protection depends on the plugin's
   cooperation. Most work; adds a small, general broker to the core.
5. **A skill only.** Skills are instructions, and Cairnward never runs their scripts
   (`skills.py`). A skill can't hold the engine, so this isn't an option on its own. It does fit
   on top of option 3 or 4.

## Decision
**Option 4, with a skill for the review workflow.** Research moves to its own
repository (`cairnward-research`) and runs as a plugin under the contract below. The contract is
general: any future plugin follows the same rules.

### The plugin contract
The rule behind it: **a plugin gets no capability that a protection depends on. It asks a core
service, and the service enforces the protection.** The sandbox makes sure the plugin can't go
around the services.

A plugin is an MCP server marked `kind = "plugin"` in `mcp.toml`. For a plugin:

1. **Least-privilege manifest.** Uses the existing `permissions` (`network`, `read`, `write`,
   `env`), with stricter defaults:
   - `network = []` by default. Outside requests go through the fetch service (rule 3).
   - `read` is required, so the home folder is denied apart from the folders listed.
   - `write` only inside `{data}`.
   - Direct `network` entries are an exception the user approves at install. They are
     shown and audited. A loopback entry, such as Zotero's `127.0.0.1:23119`, must name an
     exact address and port.
2. **Sandbox required.** A plugin starts only in the sandbox, whatever the environment's
   `isolation` setting. Running one unsandboxed is a separate, explicit switch (D55: every
   protection can be switched off). Turning it on shows a privacy warning and the dashboard
   highlight, and is audited.
3. **Brokered services only.** The plugin is given a per-launch key, held in memory and
   revoked when the plugin stops, and pinned to the environment it was started in. A run's key
   already gets this rule (`outside_run_environment` in `gateway/app.py`). The services:
   - **Models:** the gateway, as for any client. Calls are audited, budgeted and redacted.
   - **Fetch:** `POST /v1/cairnward/fetch` with the method, URL, headers and body. The core
     checks the destination as `net.connect` in the plugin's environment and runs redaction on
     the outgoing text. It then sends the request through egress, records a digest of the
     request, and returns the response bytes without parsing them. Hosts are limited to the
     plugin's declared `fetch_hosts`, inside the environment's network policy. A size limit
     and a per-plugin rate limit apply.
   - **Artifacts:** writes go to `plugin/<name>/<id>` in the artifacts store, through the same
     path and `rev` checks as today. A plugin can only reach its own area. Locking and backup
     cover it like other artifacts.
4. **Pinned code.** The plugin's installed package is pinned by hash. This extends `pin_file`
   and `sha256` (`mcp/launch.py`) from one file to the package's lock, as is already done for
   Presidio and PaperQA2. A changed hash stops the plugin until the user reviews the change.
5. **Pinned tools.** Unchanged: tool definitions are pinned and scanned for suspicious text
   (`mcp/pins.py`). Pinning can't be turned off for plugins without the same explicit,
   audited switch as rule 2.
6. **Output is untrusted.** The MCP proxy already records each tool result in provenance
   (`mcp/hub.py`). For plugins, the policy engine also treats any risky action that follows
   plugin output in the same conversation as needing approval, with the plugin named in the
   prompt.
7. **Conformance tests.** A plugin must pass a contract test suite before the core starts
   it, built on `tests/test_mcp_conformance.py`:
   - it starts with no network;
   - outside requests only succeed through fetch;
   - writes outside `{data}` and its artifacts area fail;
   - its key is refused in another environment;
   - its tool definitions match the pins.

   The suite ships with the core. A plugin's CI runs it against the core version it
   declares.
8. **Versioned contract.** The services in rule 3 carry a contract version. A plugin declares
   the range it supports, and the core refuses a plugin outside it.

### What moves to `cairnward-research`
- `src/cairnward/research/`, except Presidio (below), with its tests, evals
  (`evals/pdf_qa`, `evals/citation_existence`, `evals/litreview_table`), the PaperQA2 spike and
  the vendored CSL files with their manifest.
- The `library`, `research`, `lit` and `cite` commands, as the package's own CLI.
- FR-RES-1 to FR-RES-13, spec §4.6, D63 and D67, and ADR-0014.
- The research database and its migrations, the job store and the job runner. Job output is
  saved as artifacts (rule 3).

### How research runs as a plugin
- **Tools:** `lit_search`, `lit_ask`, `lit_find`, `lit_review_start`, plus `lit_job_status`
  and `lit_job_result`. Long reviews are started and polled. They never run inside one MCP
  call.
- **Academic sources:** OpenAlex, Semantic Scholar, arXiv, Crossref and PubMed go through the
  fetch service, with their hosts listed as `fetch_hosts`. Online sources stay off by default,
  as today (`research_sources`).
- **Zotero:** fetch only allows outside hosts, so Zotero's local API is the one direct
  `network` entry (`127.0.0.1:23119`), approved by the user under rule 1.
- **PaperQA2 and Docling** run inside the plugin's own sandbox, from virtual environments in its
  `{data}` folder. A nested sandbox isn't needed, and likely isn't possible inside bubblewrap.
  PaperQA2's model calls already go through the gateway.
- **A skill** (`skills/literature-review/SKILL.md`) describes the review workflow: search,
  screen, ask, then the table and references. Any chat client or agent can follow it with the
  tools above.
- **Without Cairnward**, the package can be used as a standalone MCP server, for example as a
  Claude Code plugin. That use has none of the contract's protections, and the package's
  README must say so.

### What the core must provide first
1. **Plugin keys** for the gateway: issued at launch, pinned to the environment, revoked at
   stop. The gateway's address is added to the plugin's sandbox allow-list.
2. **The fetch service** (rule 3), with tests against a real local HTTP server through a real
   egress proxy, not a mock transport (CLAUDE.md, testing lessons).
3. **Presidio stays in the core.** Move `research/engines/presidio.py`, its worker and its lock
   to `cairnward/redaction_engines/`. It's a redaction feature (FR-RED-2), not a research one.
4. **The plugin artifacts area** (rule 3).
5. **Rules 1, 2, 4, 6, 7 and 8** in the MCP launcher, the policy engine and the test suite.

### What the core loses
- The research hooks in `core/server.py`, `core/management.py`, `egress/control.py`,
  `mcp/hub.py`, `policy/engine.py` (the `lit_*` special cases), `cli.py`, `config/models.py`,
  `doctor.py`, `check.py` and `run/runner.py`.
- The research database from the core's migrations, and `include_research` from the backup
  settings.
- The dashboard's jobs view. It can come back as a generic view of what a plugin reports.

## Consequences

### Security after the split
Where each kind of issue sits once research is a plugin:

1. **Stays in the core.** Everything that enforces protection: gateway authentication, policy,
   egress, sandbox settings, the audit log, the vault, the dashboard. The core shrinks: the
   egress process stops launching research workers, and the core stops parsing PDFs. The
   2026-11-04 review's finding on plain-HTTP egress (the client's `Host` header is passed
   through unchecked, `egress/proxy.py`) still applies, and also matters for the fetch service,
   which must set `Host` from the URL itself.
2. **Moves with the plugin.** The PaperQA2 worker's `pickle` cache, the search cache (plain
   text, no size limit), and parser bugs in pypdfium2, Docling or GROBID. They still need
   fixing, but they are contained by the plugin's sandbox instead of running in the core.
3. **New in the core.** Plugin keys, the fetch service and the artifacts area are new attack
   surface. Fetch handles internet responses, but only passes bytes through, which is a much
   smaller risk than the in-core PDF parsing it replaces. These parts get the closest review.
4. **Not solved by any contract.**
   - *Data leaving through allowed sources:* the plugin legitimately sends your questions to
     academic APIs. Fetch makes redaction mandatory, but redaction only catches what it
     recognises, and Presidio's small spaCy model misses some names.
   - *Prompt injection from papers:* rule 6 makes it visible and gates the risky actions that
     follow, but a model can still be misled. An injection test corpus is still undone
     (handoff 2026-11-04).
   - *Correctness:* made-up citations and misquoted pages remain the plugin's job. The citation
     and quotation checks move with it and must stay.
   - *Standalone use:* outside Cairnward, none of the above applies.

### Other consequences
- **The core's scope narrows.** §1 and goal G5 become "research workflows are provided by a
  separate plugin". D42 and D43 are amended, not dropped. The spec's title loses "and research
  agent". The threat model gains the plugin contract and section 4 above.
- **New general features:** any MCP server can be run as a plugin, with model calls, outside
  requests and durable output brokered by the core.
- **Harder:**
  - Two repositories and two releases. The research package depends on the contract version
    (rule 8).
  - Calls pass through the MCP proxy, the fetch service and the sandbox's network route. This
    adds some overhead per call, probably small next to a CPU model call; measure it.
  - `research.search` and `research.fetch` audit records become fetch-service records plus
    `tool.call` records.
  - Fetch suits simple HTTP APIs. A plugin needing another protocol needs a direct `network`
    entry, approved under rule 1.
- **Results must be measured again.** The `lit.ask` 8/8, screening 12/12 and PaperQA2 q1
  results were measured on the in-core version. Run the full `cairnward check` on `main`
  before the split (the handoff's step 1) as the baseline. Afterwards, run the same evals
  through the plugin on the reference machine.
- **Migration for the owner:** `cairnward-research import` copies existing libraries, jobs and
  the research database from the old data folder once. Locked libraries
  (`library-locks/*.age`) are copied as they are, since the key wrapping doesn't change.

### Order of work, if accepted
1. Baseline: the full check on `main` (handoff step 1).
2. Move Presidio into the core (step 3 above). Worth doing even if the split stops here.
3. Plugin keys, the fetch service and the plugin artifacts area (steps 1, 2 and 4).
4. The rest of the contract: manifest defaults, required sandbox, package pinning, provenance
   gating, conformance suite, contract version.
5. Create `cairnward-research`. Move the code, tests and evals. Build the plugin, the skill
   and the import command.
6. Remove the research hooks from the core. Update the spec, the requirements, the threat
   model and the README.
7. Run the evals through the plugin on the reference machine and compare with the baseline.

### Open questions, as decided
- The workflow YAML runner moved with research. It only runs research steps.
- Backups stay in the outer process for now. Moving them is separate work with its own risk, and it
  doesn't affect the plugin.
- No job page in the dashboard. The plugin's tools and CLI report jobs; a generic view can come later.
- Plugins come only from a path the user names. A list of known plugins needs someone to publish it.

### Where the build differs from this ADR
- **Repository name:** the repository is `literature-review-agent-plugin`. The package and its
  command stay `cairnward-research`, which is the name this ADR uses throughout.
- **Rule 1 is narrower:** a direct `network` entry must be an exact local address and port. An
  outside host is refused, because the sandbox would reach it around egress, so "another protocol"
  isn't supported in contract 1.
- **Rule 3 has one more service:** item keys, so a locked library's key stays with the
  conversations' keys and the recovery key still covers it. Without it, locked libraries would have
  needed re-locking, and the ADR's migration note would be wrong.
- **Rule 3's fetch limits hosts by `fetch_hosts`,** so an open-access PDF on a repository not listed
  is refused until you add the host. Before, it asked. This is stricter and less convenient.
- **Rule 4 pins a folder,** the installed code, instead of a lock file. A lock's hash says nothing
  about what's on disk now.
- **Rule 7:** a new tool isn't a failure, because the hub offers it to no one until `cairnward mcp
  approve`, which needs the plugin to start. A changed one is.
- **A plugin's key can't use the MCP endpoint.** The ADR didn't cover this; review found it.
- **The CLI runs in the sandbox too,** through `cairnward plugin run`, so parsing a PDF by hand gets
  the same limits. Only engine installs, Drive and moving downloads stay outside.
- **Research's own audit records are gone** (`research.ask`, `library.index`, `job.*`): the core
  records its model calls, fetches and tool calls. A plugin's own account of itself isn't evidence.
