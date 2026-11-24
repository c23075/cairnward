# Project status

Last updated 2026-11-25. This is the owner's view of where Cairnward stands. Pending work, for you
and for agents, is kept in one list with Backlog.md, in [backlog/](../backlog/) (ADR-0022): the
"Decisions and actions", "Next for agents" and "Testing still to do" sections below are generated
from it, and CI fails if they fall behind. To see it as a board, run
`tools/backlog/node_modules/.bin/backlog browser`. What each session did is in the
[progress log](plan/progress.md); requirement by requirement, see
[requirements-status.md](plan/requirements-status.md).

## Summary
The planned code is built, and most of what remains is testing or needs you. **It isn't production
ready yet:** the code and its tests are in good order, but the backup has never been restored, it
has run on real WSL2 only once (2026-11-16), and nobody but the assistant that wrote it has reviewed
it. Those are below.

Literature research is a separate project with its own status page: `docs/status.md` in
[`literature-review-agent-plugin`](https://github.com/c23075/literature-review-agent-plugin)
([ADR-0017](adr/0017-research-as-a-plugin.md)). Of the 106 requirements tracked (93 here and 13 there):

| State | Count |
|---|---|
| Done and covered by tests or measurements | 84 |
| Built, but only tested against a stand-in | 14 |
| Partly done | 6 |
| Left out by decision (Ollama-compatible endpoint, Anthropic driver) | 2 |

WSL2 ran on a real Windows laptop on 2026-11-16: isolation held, and a Windows chat app worked through the gateway.

**Machines in use:** a Debian desktop and a Windows laptop. All the optional features are planned
for use, so all their real-world checks below apply.

## Decisions and actions for the owner
Ask an agent "what's next?", or run `uv run python scripts/tasks.py next --kind owner`.
High priority is the next thing to do, then Medium, then Low. An ID never changes, so it can be
quoted in commits and the progress log; to reorder, change the priority. IDs before 2026-11-25
were A, W and C numbers ([map](plan/progress.md#ids-before-2026-11-25)).

<!-- tasks:owner -->
<!-- Generated from backlog/ by scripts/tasks.py; change tasks with `backlog`, not here. -->
| # | Priority | What | Why it matters |
|---|---|---|---|
| TASK-1 | High | Set up restic to a real remote | The only backup is on the same disk as the data |
| TASK-2 | High | Keep the backup password somewhere off this machine | Without it, backups can't be restored after a disk failure |
| TASK-3 | High | Try one real restore | The real repository has never been restored |
| TASK-4 | High | Run the first signed release | 0.1.0 is prepared: version, lockfile and changelog. After merging, tag `v0.1.0`, check the draft's signatures and publish ([steps](repository-management.md#9-releases-and-versions)). The workflow has never run on GitHub |
| TASK-5 | High | Protect `main` with a ruleset | Require a pull request and the CI checks; block force pushes and deletion. Nothing stops a merge before CI passes; PRs 22 to 24 broke `main` (I25). Use a ruleset, not classic protection, so Scorecard can read it ([steps](repository-management.md#1-protect-the-default-branch)) |
| TASK-6 | High | Turn on secret scanning and push protection | Settings → Code security, with Dependabot alerts. Push protection stops a secret at `git push` even where the pre-commit hook isn't installed |
| TASK-7 | High | Accept or reject ADR-0022, one task list and a progress log | Proposed in PR 69 ([ADR](adr/0022-one-task-list-and-a-progress-log.md)) |
| TASK-8 | Medium | Turn on disk encryption on both machines (LUKS, or BitLocker on Windows) | Protects unlocked conversations if a disk is stolen |
| TASK-9 | Medium | Find an outside reviewer | Only the assistant that wrote the code has reviewed it |
| TASK-10 | Medium | Try the Rust egress data path for real | Set `[egress] implementation = "rust"` with your real agents and the sites they use, TLS inspection included (build with `cargo build --release --manifest-path rust/Cargo.toml`). ADR-0020 switches the default only after a real run; it's only been tested against local stand-ins |
| TASK-11 | Medium | Rerun `cairnward doctor` on the laptop under WSL2 | I42's fix (the probe checks the route to Windows) hasn't been rerun on the laptop |
| TASK-12 | Medium | Move your server to the research plugin | Your running server and data are still on the old setup. Restart `cairnward serve` from `main`, take a backup, then run `cairnward-research install-plugin` and `cairnward-research import`. The import has only run in tests ([plugins](plugins.md), [operations](operations.md)) |
| TASK-13 | Medium | Turn on "Automatically delete head branches" (Settings → General) | Merged branches pile up; PR 39's branch was still there after it merged |
| TASK-14 | Medium | Install the pre-commit hooks on both machines | `uv sync && uv run pre-commit install` on the desktop and the laptop. The secret scan runs only where it's installed. After PR 68 merges |
| TASK-15 | Medium | Read Scorecard's first findings (Security → Code scanning) and fix the cheap ones | After PR 68 merges and the workflow has run |
| TASK-16 | Medium | Limit the client keys `doctor` warns about | Run `cairnward doctor`, then set `environments = [...]` in `clients.toml`, or rerun `cairnward client add <name> --environment <env>`. Keys made before D86 can use any environment, including `scratch`, which is more open than `coding` (I39) |
| TASK-17 | Low | Try the OpenShell sandbox, if you want it | Install OpenShell, set `OPENSHELL_TELEMETRY_ENABLED=false` for its gateway, and try one run with `sandbox = "openshell"`. It has only run against a stand-in. Whether a sandbox can reach Cairnward's loopback-only ports is unknown |
| TASK-18 | Low | If you turn on TLS inspection: try it with your real agents and the sites they use | Only tested with httpx and curl against a local server. Agents that pin certificates will fail |
| TASK-19 | Low | Run `scripts/bench_startup.py --existing` on the desktop | Confirms I15's start-time fix on the reference machine; so far measured only in a cloud session |
| TASK-20 | Low | Delete the merged branches | 12 were left on 2026-11-06, all contained in `main`. An agent session can't delete them ([commands](repository-management.md#2-branches)) |
| TASK-21 | Low | Allow only merge commits (Settings → General) | Every merged PR so far used one; turning off squash and rebase keeps the history consistent |
| TASK-22 | Low | Accept or reject ADR-0021, managed mode for Cairnsteward | Proposed 2026-11-11; nothing is built until it's accepted ([ADR](adr/0021-managed-mode.md)) |

Done:
- **TASK-54** (2026-11-16): WSL2 checklist on the laptop. Done except replication (step 7), which is TASK-31. Found I42 ([results](wsl2.md#results))
- **TASK-53** (2026-11-10): Decide on the Rust rewrite. Go ahead (ADR-0020)
- **TASK-52** (2026-11-10): Decide how the database key is protected. Wrapped with a passphrase, with an optional reference for unattended starts (option c)
- **TASK-51** (2026-11-05): Delete the temp eval folders and old worktrees on the desktop
- **TASK-50** (2026-11-05): Review and merge PR 28
- **TASK-49** (2026-11-05): Decide about personal email addresses in the public commit history. They stay, by your decision: they're public information anyway. History won't be rewritten
<!-- /tasks:owner -->

## Next for agents
<!-- tasks:agent -->
<!-- Generated from backlog/ by scripts/tasks.py; change tasks with `backlog`, not here. -->
- **TASK-23** (High): Test replication with encrypted databases (D80). `replication.py` opens the vault through `Vault.open`, so it should work, but there's no test
- **TASK-24** (Medium): Pin the Rust binary by hash, as plugins are pinned (Rust review R4)
- **TASK-25** (Medium): Check the threat model and limitations at each milestone. Against the code, recorded in `docs/reviews/`. Not yet a habit (NFR-S2). The last check, 2026-11-10, found two wrong claims; `limitations.md` still said WSL2 had never run until 2026-11-24
- **TASK-26** (Low): Let `cairnward audit verify --rust` call `cairnward-audit-rs` when it's built. Today an auditor runs the binary by hand ([operations.md](operations.md))
- **TASK-27** (Low): Map the threat model to the OWASP Top 10 for Agentic Applications. It maps to the Top 10 for LLM Applications only ([limitations](limitations.md))
- **TASK-28** (Low): Audit MCP's environment refusals, as chat and embeddings refusals are. Found in the 2026-11-24 review (R5): MCP answers 403 for a run's or a client's other environment but records nothing
- **TASK-67** (Low): Check whether the 2026-11-16 WSL2 run covered I1, and update I1 and wsl2.md. I1 and wsl2.md's known gaps say the VM-socket fix hasn't run on real WSL2, but on 2026-11-16 `doctor`, which checks it, passed every isolation check there ([results](wsl2.md#results))
- **TASK-29** (waiting on TASK-10): Switch the egress default to Rust. Then remove the Python data path, keeping the decision service. Only after the owner's real run (ADR-0020)
<!-- /tasks:agent -->

## Known issues
Outside reports arrive as GitHub issues. One that changes the project's state also gets an I-number here, and each refers to the other (e.g. #36 and I35). See [CONTRIBUTING.md](../CONTRIBUTING.md).

### Security and privacy
| # | Issue | State |
|---|---|---|
| I1 | The core's sandbox could open VM sockets. Under WSL2 these could reach Windows past the network rules (T-28) | Fixed: the core installs its own seccomp filter at start, refusing VM sockets and io_uring, and won't start under WSL2 without it. Shown in a real srt sandbox on Linux, where a VM socket opened before the fix and is refused after; `doctor` now checks it. Not yet run on real WSL2 (TASK-54) |
| I42 | On real WSL2 the doctor's self-test reported `windows_programs` not blocked: `/init` is registered with binfmt's `F` flag, so it starts in a sandbox although hidden. It couldn't reach Windows | Fixed 2026-11-16: the probe checks the route to Windows (`/run/WSL`, `WSL_INTEROP`; VM sockets separately). Interop off in `/etc/wsl.conf` is advised. Not yet rerun on the laptop |
| I2 | Under WSL2, any program running as you on Windows can read your Linux files | Accepted: only locking protects them |
| I3 | Backends on the local network can use plain `http://`, so prompts cross the network unencrypted | Allowed, with a warning from `doctor`, `config check`, `serve` and the dashboard |
| I4 | A third-party tool can send plain `http://` to a host you've allowed. Cairnward allows and audits it, but doesn't require encryption | Accepted |
| I5 | The first streamed reply from a model not yet known to leak reasoning still reaches the client (T-26) | Fixed: with thinking off, a model's first streamed reply holds back its answer text until `</think>` shows it was reasoning, which then goes out as `reasoning_content`. A model that answers cleanly is recorded and streams directly from then on. Remaining: a model recorded as clean that leaks later still reaches the client once, and that first reply per model arrives all at once instead of word by word |
| I7 | Run snapshots can't be locked, and locking doesn't erase earlier plaintext from disks or old backups | Accepted |
| I8 | Presidio's small spaCy model misses some names, so redaction isn't complete | Accepted |
| I26 | With TLS inspection on, the local CA's key could impersonate any site to tools that trust it | Accepted: off by default, owner-only file outside every sandbox, `cairnward egress-ca rotate` |
| I27 | TLS inspection passes request bodies on unredacted, offers only HTTP/1.1, and doesn't read WebSocket messages after the upgrade | Accepted (ADR-0018) |
| I37 | With isolation off, an agent running as you can read the TLS-inspection CA key, which can impersonate any site to tools that trust it (T-33). Found in the 2026-11-05 review (R3) | Fixed 2026-11-24 (D85): runs there are refused, by `cairnward run` and the core, unless the environment sets `allow_without_isolation`, which stays a warning and is audited. Tested against the real config loader and management app, not with the real sandbox |
| I38 | The gateway doesn't check `Origin`, so that desktop apps that send one keep working (T-01) | Accepted: the `Host` allowlist, per-client API keys and no CORS headers still refuse web pages |
| I39 | A client's own API key can name any environment, and so a more open privacy level; only run keys are pinned (spec §4.1). Noted in the 2026-11-09 audit | Fixed 2026-11-24 (D86): `environments = [...]` per client in `clients.toml`; other environments get 403, audited. New clients from `client add` get the default environment; `doctor` warns about unlimited keys. Existing keys stay unlimited until you limit them (TASK-16) |
| I40 | Backups staged a plain per-conversation export in `<state>/backup/staging` while they ran, unlocked conversations' keys included, and a failed restic run left it behind. Found by the 2026-11-10 review (R3) | Fixed: staged in memory (`$XDG_RUNTIME_DIR`, a fixed name so snapshots stay incremental) and removed even after a failure; on disk only without a memory folder (warned) or with `[backup] staging = "disk"`. Tested with real restic |
| I41 | Under load (8–16 connections at once), about 1 egress connection in 300 was dropped without an answer, on both data paths: an audit append failed with "database is locked" when a slow `fsync` held SQLite's lock past `busy_timeout`. Found by `scripts/bench_egress.py` (2026-11-10) | Fixed: appenders in one process queue on a lock with no time limit; `busy_timeout` 30 s across processes. Fail-closed before the fix: nothing was sent unrecorded |
| I32 | PR 28 changed trust boundaries (a local CA that decrypts HTTPS, OpenShell as a sandbox) without updating `docs/threat-model.md` or recording a review in `docs/reviews/`, as NFR-S2 and the PR checklist require | Fixed: threat model v0.4 and [review](reviews/threat-model-2026-11-05.md) |
| I33 | TLS inspection decrypted every program in the environment, but only `cairnward run` gives tools the CA, so MCP servers, remote restic backups, `mcp login`, plugin commands and downloads would have failed. Found by the review | Fixed before anyone turned inspection on: only runs given the CA are inspected |
| I36 | TLS inspection matched path rules against the raw path, so `/repos/me/../../other` passed an allow rule for `/repos/me/**` and `//admin` or `/%61dmin` slipped past a deny rule. Found by an audit (2026-11-09) | Fixed: paths are normalized before matching; an encoded slash, backslash or NUL is refused. A request must also be allowed with its `;` parameters dropped (`/a/..;/b`) and ignoring case. Tested against the rule checker, not a real server. Threat model v0.5 (T-37) and [review](reviews/threat-model-2026-11-09.md) |
| I34 | Domain fronting: an inspected request could name another site in `Host` than the tunnel was opened to, and a shared CDN would serve it, around the host allow list and path rules. Found by the review | Fixed for inspected tunnels. Uninspected tunnels can't be checked (T-34) |
| I30 | On native Windows, curl ignores Cairnward's CA bundle (it uses the Windows certificate store), so TLS inspection would break it there. Found by CI | Accepted: runs on Windows go through WSL, where curl uses the bundle |

### Functionality
| # | Issue | State |
|---|---|---|
| I9 | Restoring locked job, run and library files was by hand | Fixed: `backup restore` brings them back, tested with real restic |
| I10 | Scheduled backups and replication stop when WSL goes idle | Accepted: documented |
| I11 | `keyring:` references usually fail under WSL. Use `env:` | Accepted: documented |
| I13 | Audit anchoring is off, so there's no outside proof of the audit log | Your choice |
| I28 | Egress serves one plain-HTTP request per connection but didn't tell the client, so a keep-alive client's second `http://` request on that connection failed. Worse, bytes after the first request were piped to its host unread, so a pipelined second request could reach that host without its own policy check. HTTPS (CONNECT) wasn't affected | Fixed: egress passes exactly one request and its response through h11, answers `Connection: close`, and drops anything after it. A request with two `Host` headers gets 400. Tested with a keep-alive client and a raw origin that records every byte |
| I35 | The dashboard's backup view failed with a 500 when the repository was an unsupported kind such as `sftp:`, instead of saying why it can't be used. Reported on GitHub (#36) | Fixed: the same-disk check tolerates an unusable repository, and the view shows the problem, with a remote's host only |
| I29 | With `sandbox = "openshell"` the agent works on a copy: large workspaces are slow to copy, and a failed copy back leaves the work in the kept sandbox | Accepted (ADR-0018) |

### Performance
| # | Issue | State |
|---|---|---|
| I14 | Strict overhead missed its 50 ms target | Closed: target raised to 75 ms, or 150 ms with isolation. Measured 59 ms and 105 ms |
| I15 | Start time missed the 3 s target when isolated: about 3.5 s until `/health` answers on the desktop on 2026-11-05 | Fixed in code, to be confirmed on the desktop: the core no longer imports the MCP SDK until an MCP server or `/mcp` is used, and databases already up to date skip alembic. In a cloud session (a different machine, isolated, Strict), the median fell from 2.6–2.9 s to 2.1–2.2 s on empty data, and from 2.8 s to 1.9–2.3 s on existing data. Run `scripts/bench_startup.py --existing` on the desktop to confirm |

### Unexplained or uncovered
| # | Issue | State |
|---|---|---|
| I16 | One slow fast-model run in the first-token measurements | Unexplained |
| I18 | `core/server.py` isn't in the coverage count. Only the isolation job runs it | Accepted |
| I19 | CI doesn't run WSL2, so a change that breaks it only shows up on a real machine | Accepted |
| I25 | `main`'s CI fails: a Windows path in a test's TOML, git missing in the Debian job, and `redaction_engines` and `plugins` under the 80% coverage minimum | Fixed in PR 25: `main` passes CI, including Windows |
| I31 | PR 28's first CI run failed: its new tests assumed git, Unix sockets and curl's CA handling | Fixed in PR 28 before it merged: the second run passes on Linux and Windows |

## Testing still to do
<!-- tasks:checks -->
<!-- Generated from backlog/ by scripts/tasks.py; change tasks with `backlog`, not here. -->
### Could reveal real bugs
- [ ] Qwen Code, opencode and Goose with the home folder hidden (TASK-30)
- [x] Full check on `main`, 2026-11-04: Overall fail (report on the `dev` branch). The coding eval passed; research results are in the plugin's repository. Chat clients were skipped because none has made a model call yet (TASK-61)
- [x] Aider's full task with the home folder hidden, 2026-11-05: Passed 3 of 3 (228, 459 and 564 s). Aider's request to `raw.githubusercontent.com` was denied each run and did no harm (TASK-62)
- [x] WSL2 checklist on the laptop, 2026-11-16: Replication not tried (TASK-31) (TASK-66)

### Never run for real
- [ ] Replication between the desktop and laptop: Through real Syncthing or a NAS, and on its schedule under an isolated server. (TASK-31)
- [ ] Locking jobs and runs through a live isolated server (TASK-32)
- [ ] OAuth token refresh inside the sandbox (TASK-33)
- [ ] A run with `sandbox = "openshell"` against a real OpenShell gateway (TASK-17) (TASK-34)
- [ ] TLS inspection with real agents and outside sites (TASK-18) (TASK-35)
- [x] Locking a conversation and its artifacts from the dashboard on a live isolated server: Found and fixed a bug (`tests/isolation/test_owner_flows.py`) (TASK-58)
- [x] Dashboard settings and "back up now" in isolated mode: Settings never worked there, now fixed (`tests/isolation/test_owner_flows.py`) (TASK-59)
- [x] Locking a plugin's item (a research library) through a live isolated server, 2026-11-05 (TASK-63)
- [x] The `cut_off` warning, 2026-11-05: A real model limit returned `finish_reason` `length` through the gateway, and a workflow reported it (TASK-64)
- [x] Start time on a large data folder, 2026-11-05: 3,000 conversations (23 MB) start as fast as empty data (I15) (TASK-65)

### Project-level
- [ ] Release workflow on GitHub (TASK-4) (TASK-36)
- [ ] The systemd unit under real systemd: `deploy/systemd/cairnward.service`; only `systemd-analyze verify` so far. (TASK-37)
- [ ] A plugin other than research and a test plugin running the contract (TASK-38)
- [ ] Outside review (TASK-9) (TASK-39)
- [ ] Manual accessibility: keyboard only, a screen reader, 200% zoom (TASK-40)
- [x] Clean install as a new user, following the READMEs: From the public repositories, with an empty home on this machine. The core worked, and the plugin's steps needed a fix. Not a fresh VM, so system packages weren't tested (TASK-60)

### Features tested only against a stand-in
- [ ] Chat clients: Open WebUI, Jan, LibreChat (TASK-41)
- [ ] restic to a real remote (TASK-1) (TASK-42)
- [ ] A private remote for audit anchoring, and a public timestamp server (TASK-43)
- [ ] Hosted model providers (TASK-44)
- [ ] A real remote MCP server and OAuth provider (TASK-45)
- [ ] `llama-server` on the desktop and laptop (TASK-46)
- [ ] Container templates with Docker or Podman on the desktop and laptop (TASK-47)
- [ ] The Drive mirror through real rclone (FR-SYNC-2) (TASK-48)
<!-- /tasks:checks -->

## How connections are secured
Read from the code. Connections to real outside services haven't been tried yet.

| Connection | Protection |
|---|---|
| Chat app or agent to Cairnward | Plain HTTP with a key, on this machine only. Cairnward refuses to listen on anything but loopback, so this never reaches a network |
| Windows app to Cairnward in WSL2 | The same, through WSL's `localhost` forwarding. Worked with Chatbox on 2026-11-16 |
| One device to another | No direct connection. Replication goes through a shared folder, and its files are encrypted before they get there |
| Cairnward to a hosted model | HTTPS required, with certificates verified |
| Cairnward to a remote MCP server | HTTPS required; `http` only on this machine |
| Cairnward to the academic APIs | HTTPS |
| Agents and tools to the internet | Only through the egress proxy, to allowed hosts. HTTPS passes through encrypted, so Cairnward sees the host but not the content, unless the environment turns on TLS inspection (off by default; then egress decrypts and re-encrypts it, I26). Plain HTTP to an allowed host is also passed on (I4) |
| Cairnward to a model on your network | HTTP is allowed but warned about (I3). HTTPS or a tunnel keeps it encrypted |
| Backups | restic encrypts them before they leave |

## Recent changes
- I37 and I39 fixed (2026-11-24, D85, D86): TLS inspection needs isolation unless you allow it, and a client's own key can be limited to some environments, with new clients limited to the default one. Threat model v0.8 and [review](reviews/threat-model-2026-11-24.md)
- Repository (2026-11-25): the task list moved to Backlog.md, in [backlog/](../backlog/), with a board (`backlog browser`), new `TASK-n` IDs, and a CI job that checks every task and this page (ADR-0022 revised, D84)
- Repository (2026-11-24): pending work in one list, then `plan/tasks.json`, with this page's action, agent and testing sections generated from it, and a [progress log](plan/progress.md) instead of dated handoffs (D84, [ADR-0022](adr/0022-one-task-list-and-a-progress-log.md), proposed). Before it, in PR 68: AGENTS.md for every coding agent, a pre-commit secret scan and an OpenSSF Scorecard workflow (D83)
- Read-only views for an observer (2026-11-11, D82): `GET /v1/runs` and `GET /v1/audit/since` on the management socket, used by [Cairnsteward](https://github.com/c23075/cairnsteward) (formerly Cairnward Keep), the fleet control plane (the enterprise plan's fleet view). Tested in `tests/test_run.py`, and by its end-to-end test against two real `cairnward serve` instances (isolation off). It only reads; it never writes to the socket
- Managed mode proposed (2026-11-11, [ADR-0021](adr/0021-managed-mode.md), **proposed**): an opt-in, reversible way for an instance to be governed by Cairnsteward, which is now planned as an enterprise control plane for users and model risk management. Personal mode stays unchanged. Nothing built
- Rust plan finished (2026-11-10, ADR-0020): an independent audit verifier in Rust instead of a writer (stage 4, D81); measured at the same cost as Python ([bench](bench/results-2026-11-10-egress-rust.md)) and reviewed ([review](reviews/rust-data-path-2026-11-10.md), threat model v0.7: the decision socket now needs a secret). Measuring found I41 (fixed). The default stays Python until you have run it for real (A18)
- Rust egress data path, stage 2 (2026-11-10, opt-in `[egress] implementation = "rust"`): the proxy in Rust asking the Python decision service; every egress and egress-fuzz test runs against both paths (one difference found and fixed), and `serve` with it is tested with the real sandbox. TLS inspection in Rust added the same day (stage 3), tested with the same tests as the Python path
- Rust rewrite, stage 1 (2026-11-10, [ADR-0020](adr/0020-rust-egress-and-audit.md)): a `rust/` workspace with the address checks ported and checked against Python on 12,000 generated addresses, a CI job, and the Python decision service the Rust proxy will ask (`egress/decisions.py`). Stage 1 is done; nothing in production uses Rust yet
- Database encryption built (2026-11-10, D80, [ADR-0019](adr/0019-sqlcipher-encrypt-everything.md)): `cairnward store encrypt`, a passphrase-wrapped key, optional key by reference (A16: option c). Tested in isolated mode with the real sandbox; not yet with replication. Threat model v0.6 and [review](reviews/threat-model-2026-11-10.md): three findings fixed (the key readable in `/proc`, an `env:` reference inherited, backups staged on disk: I40). Not yet tried in isolated mode with the real sandbox, or with replication
- Merkle checkpoints built (2026-11-10, D79): anchors sign a Merkle root; `audit prove` and `audit check-proof` for single records. Tested with a local git repository and real `ssh-keygen`, not yet with a real remote
- Trade-offs written down (2026-11-10): I37–I39 (CA key readable with isolation off, no `Origin` check on the gateway, client keys not pinned to an environment); deferred work (Rust rewrite, Merkle checkpoints, OWASP agentic mapping) in [limitations](limitations.md); "Phase 3" now reads "Later" (spec §9)
- Docs reorganised (2026-11-09): an index by reader ([docs/README.md](README.md)), an [overview](overview.md) with where Cairnward sits in an agentic stack, the enterprise, LLMOps and model risk management plan moved to [plan/enterprise-poc.md](plan/enterprise-poc.md), and the spec's six longest paragraphs split into labelled points (no wording removed)
- Audit fixes (2026-11-09): TLS-inspection path rules normalize the path (I36); IPv6 allow-list entries without a port now match; doc drift corrected
- Repository: CONTRIBUTING.md, issue forms, Dependabot for the npm lockfiles, and a rule linking GitHub issues to I-numbers
- Docs: [managing the repository](repository-management.md), with a checklist of GitHub settings and housekeeping still to do
- Docs: the spec's architecture diagram redrawn to match the two-process layout; [limitations](limitations.md) in one place; a [guide for regulated companies](compliance-governance.md); two wrong claims in the threat model corrected (a plaintext search index and an audit retention setting, neither of which exists)
- **PR 28 (merged):** TLS inspection in egress and OpenShell as a second sandbox (ADR-0018, D77 and
  D78), both opt-in per environment. They were "not planned" in the handoff until you asked for them
- Research issues, its checklists and A2 moved to the plugin's status page, and the core no longer ships its environments (D76)
- **PR 22:** ADR-0017 accepted, and research moved to a plugin, with a plugin contract in the core
  (merged before CI could run, I25)
- Operations guide, security policy, changelog and a systemd unit added (in PR 22)
- Real-model evals and the injection corpus run through the plugin; a rule against following paper
  text added to every prompt over papers
- PaperQA2's prompts changed, and its refusal detection and the q5 check fixed (in PR 22)
- **PRs 19 to 21:** WSL2 support and the review fixes (merged)
- **PR 18:** home folder hidden from agent runs (merged)
- Full check run on `main` on 2026-11-04
