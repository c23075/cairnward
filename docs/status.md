# Project status

Last updated 2026-11-05, after PR 28 (TLS inspection and the OpenShell sandbox, ADR-0018) was
merged. This is the owner's view of where Cairnward stands. Detail on each
requirement is in [requirements-status.md](plan/requirements-status.md), and the
[handoff](plan/) is working notes for agents.

## Summary
The planned code is built, and most of what remains is testing or needs you. **It isn't production
ready yet:** the code and its tests are in good order, but the backup has never been restored, it
has never run on real WSL2, and nobody but the assistant that wrote it has reviewed
it. Those are below.

Literature research is a separate project with its own status page: `docs/status.md` in
[`literature-review-agent-plugin`](https://github.com/c23075/literature-review-agent-plugin)
([ADR-0017](adr/0017-research-as-a-plugin.md)). Of the 104 requirements tracked (91 here and 13 there):

| State | Count |
|---|---|
| Done and covered by tests or measurements | 82 |
| Built, but only tested against a stand-in | 14 |
| Partly done | 6 |
| Left out by decision (Ollama-compatible endpoint, Anthropic driver) | 2 |

The biggest unknown is whether it works on Windows, because WSL2 support has never run on real WSL2.

**Machines in use:** a Debian desktop and a Windows laptop. All the optional features are planned
for use, so all their real-world checks below apply.

## In progress
| Item | State |
|---|---|
| Moving your server to the research plugin | Your running server and data are still on the old setup |

To move: restart `cairnward serve` from `main`, take a backup, then run `cairnward-research
install-plugin`, which also creates its `litreview-*` environments, and `cairnward-research import`. The import has only run in tests, never on real
data. See [plugins](plugins.md) and [operations](operations.md).

## Decisions and actions for the owner
| # | What | Why it matters |
|---|---|---|
| A1 | Run the WSL2 checklist on the laptop ([guide](wsl2.md)) | The first real evidence for Windows |
| A3 | Set up restic to a real remote | The only backup is on the same disk as the data |
| A4 | Keep the backup password somewhere off this machine | Without it, backups can't be restored after a disk failure |
| A5 | Try one real restore | The real repository has never been restored |
| A7 | Turn on disk encryption on both machines (LUKS, or BitLocker on Windows) | Protects unlocked conversations if a disk is stolen |
| A8 | Run the first signed release | 0.1.0 is prepared: version, lockfile and changelog. After merging, tag `v0.1.0`, check the draft's signatures and publish ([steps](repository-management.md#9-releases-and-versions)). The workflow has never run on GitHub |
| A10 | Find an outside reviewer | Only the assistant that wrote the code has reviewed it |
| A11 | If you want the OpenShell sandbox: install OpenShell, set `OPENSHELL_TELEMETRY_ENABLED=false` for its gateway, and try one run with `sandbox = "openshell"` | It has only run against a stand-in. Whether a sandbox can reach Cairnward's loopback-only ports is unknown |
| A12 | If you turn on TLS inspection: try it with your real agents and the sites they use | Only tested with httpx and curl against a local server. Agents that pin certificates will fail |

Done on 2026-11-10:
- **A16:** the database key is wrapped with a passphrase, with an optional reference for unattended starts (option c).
- **A17:** go for the Rust rewrite (ADR-0020).

Done on 2026-11-05:
- **A13:** your personal email addresses stay in the public commit history, by your decision: they're
  public information anyway. History won't be rewritten.
- **A14:** PR 28 reviewed and merged.
- **A15:** the temp eval folders and old worktrees on the desktop are deleted.

## Known issues
Outside reports arrive as GitHub issues. One that changes the project's state also gets an I-number here, and each refers to the other (e.g. #36 and I35). See [CONTRIBUTING.md](../CONTRIBUTING.md).

### Security and privacy
| # | Issue | State |
|---|---|---|
| I1 | The core's sandbox could open VM sockets. Under WSL2 these could reach Windows past the network rules (T-28) | Fixed: the core installs its own seccomp filter at start, refusing VM sockets and io_uring, and won't start under WSL2 without it. Shown in a real srt sandbox on Linux, where a VM socket opened before the fix and is refused after; `doctor` now checks it. Not yet run on real WSL2 (A1) |
| I2 | Under WSL2, any program running as you on Windows can read your Linux files | Accepted: only locking protects them |
| I3 | Backends on the local network can use plain `http://`, so prompts cross the network unencrypted | Allowed, with a warning from `doctor`, `config check`, `serve` and the dashboard |
| I4 | A third-party tool can send plain `http://` to a host you've allowed. Cairnward allows and audits it, but doesn't require encryption | Accepted |
| I5 | The first streamed reply from a model not yet known to leak reasoning still reaches the client (T-26) | Fixed: with thinking off, a model's first streamed reply holds back its answer text until `</think>` shows it was reasoning, which then goes out as `reasoning_content`. A model that answers cleanly is recorded and streams directly from then on. Remaining: a model recorded as clean that leaks later still reaches the client once, and that first reply per model arrives all at once instead of word by word |
| I7 | Run snapshots can't be locked, and locking doesn't erase earlier plaintext from disks or old backups | Accepted |
| I8 | Presidio's small spaCy model misses some names, so redaction isn't complete | Accepted |
| I26 | With TLS inspection on, the local CA's key could impersonate any site to tools that trust it | Accepted: off by default, owner-only file outside every sandbox, `cairnward egress-ca rotate` |
| I27 | TLS inspection passes request bodies on unredacted, offers only HTTP/1.1, and doesn't read WebSocket messages after the upgrade | Accepted (ADR-0018) |
| I37 | With isolation off, an agent running as you can read the TLS-inspection CA key, which can impersonate any site to tools that trust it (T-33). Found in the 2026-11-05 review (R3) | Accepted: inspection is off by default and sandboxed runs can't read the key. Refusing inspection while isolation is off is a possible fix |
| I38 | The gateway doesn't check `Origin`, so that desktop apps that send one keep working (T-01) | Accepted: the `Host` allowlist, per-client API keys and no CORS headers still refuse web pages |
| I39 | A client's own API key can name any environment, and so a more open privacy level; only run keys are pinned (spec §4.1). Noted in the 2026-11-09 audit | Accepted by design for now. Possible fix: an optional list of allowed environments per client in `clients.toml` |
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
### Could reveal real bugs
- [x] Full check on `main`, 2026-11-04: overall fail (report on the `dev` branch). The coding eval passed; research results are in the plugin's repository. Chat clients were
      skipped because none has made a model call yet
- [x] Aider's full task with the home folder hidden, 2026-11-05: passed 3 of 3 (228, 459 and 564 s). Aider's request to `raw.githubusercontent.com` was denied each run and did no harm
- [ ] Qwen Code, opencode and Goose with the home folder hidden
- [ ] WSL2 checklist on the laptop

### Never run for real
- [ ] Replication between the desktop and laptop, through real Syncthing or a NAS, and on its
      schedule under an isolated server
- [x] Locking a conversation and its artifacts from the dashboard on a live isolated server: found and fixed a bug (`tests/isolation/test_owner_flows.py`)
- [x] Locking a plugin's item (a research library) through a live isolated server, 2026-11-05
- [ ] Locking jobs and runs through a live isolated server
- [x] The `cut_off` warning, 2026-11-05: a real model limit returned `finish_reason` `length` through the gateway, and a workflow reported it
- [x] Dashboard settings and "back up now" in isolated mode: settings never worked there, now fixed (`tests/isolation/test_owner_flows.py`)
- [ ] OAuth token refresh inside the sandbox
- [ ] A run with `sandbox = "openshell"` against a real OpenShell gateway (A11)
- [ ] TLS inspection with real agents and outside sites (A12)
- [x] Start time on a large data folder, 2026-11-05: 3,000 conversations (23 MB) start as fast as empty data (I15)

### Project-level
- [x] Clean install from the public repositories, as a new user with an empty home on this machine, following the READMEs: the core worked, and the plugin's steps needed a fix. Not a fresh VM, so system packages weren't tested
- [ ] Release workflow on GitHub (A8)
- [ ] Moving your server and data to the research plugin (see In progress): the import has only run in tests
- [ ] `deploy/systemd/cairnward.service` under real systemd (only `systemd-analyze verify` so far)
- [ ] A plugin other than research and a test plugin running the contract
- [ ] Outside review (A10)
- [ ] Manual accessibility: keyboard only, a screen reader, 200% zoom

### Features tested only against a stand-in
- [ ] Chat clients: Open WebUI, Jan, LibreChat
- [ ] restic to a real remote (A3)
- [ ] A private remote for audit anchoring, and a public timestamp server
- [ ] Hosted model providers
- [ ] A real remote MCP server and OAuth provider
- [ ] `llama-server` on the desktop and laptop
- [ ] Container templates with Docker or Podman on the desktop and laptop

## How connections are secured
Read from the code. Connections to real outside services haven't been tried yet.

| Connection | Protection |
|---|---|
| Chat app or agent to Cairnward | Plain HTTP with a key, on this machine only. Cairnward refuses to listen on anything but loopback, so this never reaches a network |
| Windows app to Cairnward in WSL2 | The same, through WSL's `localhost` forwarding. Not yet tried |
| One device to another | No direct connection. Replication goes through a shared folder, and its files are encrypted before they get there |
| Cairnward to a hosted model | HTTPS required, with certificates verified |
| Cairnward to a remote MCP server | HTTPS required; `http` only on this machine |
| Cairnward to the academic APIs | HTTPS |
| Agents and tools to the internet | Only through the egress proxy, to allowed hosts. HTTPS passes through encrypted, so Cairnward sees the host but not the content, unless the environment turns on TLS inspection (off by default; then egress decrypts and re-encrypts it, I26). Plain HTTP to an allowed host is also passed on (I4) |
| Cairnward to a model on your network | HTTP is allowed but warned about (I3). HTTPS or a tunnel keeps it encrypted |
| Backups | restic encrypts them before they leave |

## Recent changes
- Database encryption built (2026-11-10, D80, [ADR-0019](adr/0019-sqlcipher-encrypt-everything.md)): `cairnward store encrypt`, a passphrase-wrapped key, optional key by reference (A16: option c). Not yet tried in isolated mode with the real sandbox, or with replication
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
