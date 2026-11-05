# Project status

Last updated 2026-11-05, after adding TLS inspection and the OpenShell sandbox (ADR-0018). This is the owner's view of where Cairnward stands. Detail on each
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
| A8 | Run the first signed release | Both repositories are now public, so the workflow should get as far as signing. It has never run on GitHub |
| A10 | Find an outside reviewer | Only the assistant that wrote the code has reviewed it |
| A11 | If you want the OpenShell sandbox: install OpenShell, set `OPENSHELL_TELEMETRY_ENABLED=false` for its gateway, and try one run with `sandbox = "openshell"` | It has only run against a stand-in. Whether a sandbox can reach Cairnward's loopback-only ports is unknown |
| A12 | If you turn on TLS inspection: try it with your real agents and the sites they use | Only tested with httpx and curl against a local server. Agents that pin certificates will fail |

## Known issues
### Security and privacy
| # | Issue | State |
|---|---|---|
| I1 | The core's sandbox can open VM sockets. Under WSL2 these could reach Windows past the network rules (T-28) | Open: what they can reach is unchecked |
| I2 | Under WSL2, any program running as you on Windows can read your Linux files | Accepted: only locking protects them |
| I3 | Backends on the local network can use plain `http://`, so prompts cross the network unencrypted | Allowed, with a warning from `doctor`, `config check`, `serve` and the dashboard |
| I4 | A third-party tool can send plain `http://` to a host you've allowed. Cairnward allows and audits it, but doesn't require encryption | Accepted |
| I5 | The first streamed reply from a model not yet known to leak reasoning still reaches the client (T-26) | Open |
| I7 | Run snapshots can't be locked, and locking doesn't erase earlier plaintext from disks or old backups | Accepted |
| I8 | Presidio's small spaCy model misses some names, so redaction isn't complete | Accepted |
| I26 | With TLS inspection on, the local CA's key could impersonate any site to tools that trust it | Accepted: off by default, owner-only file outside every sandbox, `cairnward egress-ca rotate` |
| I27 | TLS inspection passes request bodies on unredacted, offers only HTTP/1.1, and doesn't read WebSocket messages after the upgrade | Accepted (ADR-0018) |

### Functionality
| # | Issue | State |
|---|---|---|
| I9 | Restoring locked job, run and library files was by hand | Fixed: `backup restore` brings them back, tested with real restic |
| I10 | Scheduled backups and replication stop when WSL goes idle | Accepted: documented |
| I11 | `keyring:` references usually fail under WSL. Use `env:` | Accepted: documented |
| I13 | Audit anchoring is off, so there's no outside proof of the audit log | Your choice |
| I28 | Egress serves one plain-HTTP request per connection but doesn't tell the client, so a keep-alive client's second `http://` request on that connection fails. HTTPS (CONNECT) isn't affected. Found while testing TLS inspection | Open |
| I29 | With `sandbox = "openshell"` the agent works on a copy: large workspaces are slow to copy, and a failed copy back leaves the work in the kept sandbox | Accepted (ADR-0018) |

### Performance
| # | Issue | State |
|---|---|---|
| I14 | Strict overhead missed its 50 ms target | Closed: target raised to 75 ms, or 150 ms with isolation. Measured 59 ms and 105 ms |
| I15 | Start time misses the 3 s target when isolated: about 3.5 s until `/health` answers on 2026-11-05 (3.3 to 3.8 on empty data, 3.1 to 3.8 with 3,000 conversations). Data size makes no difference. An earlier script read 2.8 s, and why this one reads higher wasn't looked into | Open: small miss, the owner decides |

### Unexplained or uncovered
| # | Issue | State |
|---|---|---|
| I16 | One slow fast-model run in the first-token measurements | Unexplained |
| I18 | `core/server.py` isn't in the coverage count. Only the isolation job runs it | Accepted |
| I19 | CI doesn't run WSL2, so a change that breaks it only shows up on a real machine | Accepted |
| I25 | `main`'s CI fails: a Windows path in a test's TOML, git missing in the Debian job, and `redaction_engines` and `plugins` under the 80% coverage minimum | Fixed in PR 25: `main` passes CI, including Windows |

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
| Agents and tools to the internet | Only through the egress proxy, to allowed hosts. HTTPS passes through encrypted, so Cairnward sees the host but not the content. Plain HTTP to an allowed host is also passed on (I4) |
| Cairnward to a model on your network | HTTP is allowed but warned about (I3). HTTPS or a tunnel keeps it encrypted |
| Backups | restic encrypts them before they leave |

## Recent changes
- TLS inspection in egress and OpenShell as a second sandbox (ADR-0018, D77 and D78), both opt-in per environment
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
