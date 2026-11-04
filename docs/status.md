# Project status

Last updated 2026-11-05. This is the owner's view of where Cairnward stands. Detail on each
requirement is in [requirements-status.md](plan/requirements-status.md), and the
[handoff](plan/) is working notes for agents.

## Summary
The planned code is built, and most of what remains is testing or needs you. **It isn't production
ready yet:** the code and its tests are in good order, but the backup has never been restored, it
has never run on real WSL2 or real papers, and nobody but the assistant that wrote it has reviewed
it. CI has also never checked the merged code, because GitHub isn't starting its jobs (A11). Those
are in the actions below.

Research now lives in its own package, as a plugin ([ADR-0017](adr/0017-research-as-a-plugin.md)).
Of the 104 requirements tracked (91 here and 13 in the research repository):

| State | Count |
|---|---|
| Done and covered by tests or measurements | 82 |
| Built, but only tested against a stand-in | 13 |
| Partly done | 6 |
| Left out by decision (Ollama-compatible endpoint, Anthropic driver, TLS interception) | 3 |

The biggest unknowns are how accurate it is on real papers, because every result so far uses short
synthetic ones, and whether it works on Windows, because WSL2 support has never run on real WSL2.

**Machines in use:** a Debian desktop and a Windows laptop. All the optional features are planned
for use, so all their real-world checks below apply.

## In progress
| Item | State |
|---|---|
| Moving your server to the research plugin | Merged in PR 22, and the research code is on GitHub as `literature-review-agent-plugin`. Your running server and data are still on the old setup |

To move: restart `cairnward serve` from `main`, take a backup, then run `cairnward-research
install-plugin` and `cairnward-research import`. The import has only run in tests, never on real
data. See [plugins](plugins.md) and [operations](operations.md).

## Decisions and actions for the owner
| # | What | Why it matters |
|---|---|---|
| A1 | Run the WSL2 checklist on the laptop ([guide](wsl2.md)) | The first real evidence for Windows |
| A2 | Choose about 20 research papers on one question: about 14 to keep and 6 to reject. Can be done later | Measures how many relevant papers screening loses |
| A3 | Set up restic to a real remote | The only backup is on the same disk as the data |
| A4 | Keep the backup password somewhere off this machine | Without it, backups can't be restored after a disk failure |
| A5 | Try one real restore | The real repository has never been restored |
| A7 | Turn on disk encryption on both machines (LUKS, or BitLocker on Windows) | Protects unlocked conversations if a disk is stolen |
| A8 | Decide whether the repository goes public | Release signing writes its name to a public log, so the workflow stops before signing while it's private |
| A10 | Find an outside reviewer | Only the assistant that wrote the code has reviewed it |
| A11 | Fix GitHub billing, then re-run CI on both repositories | GitHub won't start any job until a failed payment or the spending limit is fixed. PR 22 was merged without CI, and its code has never run on Windows |

## Known issues
### Security and privacy
| # | Issue | State |
|---|---|---|
| I1 | The core's sandbox can open VM sockets. Under WSL2 these could reach Windows past the network rules (T-28) | Open: what they can reach is unchecked |
| I2 | Under WSL2, any program running as you on Windows can read your Linux files | Accepted: only locking protects them |
| I3 | Backends on the local network can use plain `http://`, so prompts cross the network unencrypted | Allowed, with a warning from `doctor`, `config check`, `serve` and the dashboard |
| I4 | A third-party tool can send plain `http://` to a host you've allowed. Cairnward allows and audits it, but doesn't require encryption | Accepted |
| I5 | The first streamed reply from a model not yet known to leak reasoning still reaches the client (T-26) | Open |
| I6 | A research library is plain while unlocked, its source PDFs are never encrypted, and it has no timed unlock (T-24) | Accepted |
| I7 | Run snapshots can't be locked, and locking doesn't erase earlier plaintext from disks or old backups | Accepted |
| I8 | Presidio's small spaCy model misses some names, so redaction isn't complete | Accepted |
| I20 | PaperQA2's fast model can write wrong summaries, and the main model repeats them as what a source says. The prompt change stopped the false "explicitly states" claims in the latest runs, but q3 still mentions a date no paper gives (`docs/bench/results-2026-11-05-plugin.md` in the research repository) | Open, partly fixed |
| I23 | A paper's planted instruction can still reach an answer. On the real model, one got its code into 2 of 4 lean runs, one of them with the new rule in place. The bad citation it asked for was flagged; an instruction without one wouldn't be | Open: limited, not preventable. An answer has no tools, and plugin output is gated |

### Functionality
| # | Issue | State |
|---|---|---|
| I9 | Restoring locked job, run and library files is by hand. Only conversations are imported automatically | Open |
| I10 | Scheduled backups and replication stop when WSL goes idle | Accepted: documented |
| I11 | `keyring:` references usually fail under WSL. Use `env:` | Accepted: documented |
| I12 | Only the built-in APA and IEEE citation styles work until pandoc or citeproc-js is installed | Your choice |
| I13 | Audit anchoring is off, so there's no outside proof of the audit log | Your choice |
| I21 | PaperQA2's refusals in other words weren't recognised. Wider detection now finds them, but can also mark a partial answer as a refusal | Fixed, with that risk |
| I22 | The check failed a correct "Six" on q5 because it looked for "6" | Fixed |

### Performance
| # | Issue | State |
|---|---|---|
| I14 | Strict overhead missed its 50 ms target | Closed: target raised to 75 ms, or 150 ms with isolation. Measured 59 ms and 105 ms |
| I15 | Start time is 2.8 s on empty data, against a 3 s target. A large data folder hasn't been tried | Untested |

### Unexplained or uncovered
| # | Issue | State |
|---|---|---|
| I16 | One slow fast-model run in the first-token measurements | Unexplained |
| I17 | Question q5's failure in the first eval. I22 could be the cause, but those answers weren't kept | Unexplained |
| I18 | `core/server.py` isn't in the coverage count. Only the isolation job runs it | Accepted |
| I19 | CI doesn't run WSL2, so a change that breaks it only shows up on a real machine | Accepted |
| I24 | The research test `test_cache_key_is_kept_owner_only` failed on Windows because it checked POSIX file modes, which Windows doesn't have; the code skips them there on purpose. The test is fixed and the research CI gets a Windows job, on the `windows-ci` branch there. Neither has run on Windows yet | Fix waiting on A11 |

## Testing still to do
### Could reveal real bugs
- [x] Full check on `main`, 2026-11-04: overall fail (report on the `dev` branch). Lean passed 8 of
      8, PaperQA2 6 of 8 (I20 to I22), and the coding and review evals passed. Chat clients were
      skipped because none has made a model call yet
- [ ] Real papers: accuracy, and how many relevant papers screening loses
- [x] Research evals on a real model through the plugin, 2026-11-05: answers, review and injection
      all ran, with the failures explained in the research repository's `docs/bench`
- [ ] Research workflows on a real model, then on real papers
- [ ] Aider's full task with the home folder hidden
- [ ] Qwen Code, opencode and Goose with the home folder hidden
- [ ] WSL2 checklist on the laptop

### Never run for real
- [ ] Replication between the desktop and laptop, through real Syncthing or a NAS, and on its
      schedule under an isolated server
- [ ] Locking artifacts, jobs, runs and libraries through a live isolated server
- [ ] Screening's disagreement path
- [ ] The `cut_off` warning
- [ ] Dashboard settings and "back up now" in isolated mode
- [ ] OAuth token refresh inside the sandbox
- [ ] Start time on a large data folder

### Project-level
- [ ] Clean install from the README on a fresh Debian VM
- [ ] Release workflow on GitHub (needs A8 for signing)
- [x] Injection test corpus: written and run on the real model (I23). Six cases, so it shows that an attack works, not that the others are safe
- [ ] `deploy/systemd/cairnward.service` under real systemd (only `systemd-analyze verify` so far)
- [ ] A plugin other than research and a test plugin running the contract
- [ ] Outside review (A10)
- [ ] Manual accessibility: keyboard only, a screen reader, 200% zoom

### Features tested only against a stand-in
- [ ] Chat clients: Open WebUI, Jan, LibreChat
- [ ] Academic sources: arXiv, Semantic Scholar, OpenAlex, PubMed, Crossref
- [ ] Zotero, Docling, GROBID
- [ ] Drive through real rclone
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
- **PR 22:** ADR-0017 accepted, and research moved to a plugin, with a plugin contract in the core
  (merged without CI, A11)
- Operations guide, security policy, changelog and a systemd unit added (in PR 22)
- Real-model evals and the injection corpus run through the plugin; a rule against following paper
  text added to every prompt over papers
- PaperQA2's prompts changed, and its refusal detection and the q5 check fixed (in PR 22)
- **PRs 19 to 21:** WSL2 support and the review fixes (merged)
- **PR 18:** home folder hidden from agent runs (merged)
- Full check run on `main` on 2026-11-04
