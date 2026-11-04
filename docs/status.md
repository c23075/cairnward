# Project status

Last updated 2026-11-04. This is the owner's view of where Cairnward stands. Detail on each
requirement is in [requirements-status.md](plan/requirements-status.md), and the
[handoff](plan/) is working notes for agents.

## Summary
The planned code is built, and most of what remains is testing. Of the 100 requirements tracked:

| State | Count |
|---|---|
| Done and covered by tests or measurements | 76 |
| Built, but only tested against a stand-in | 13 |
| Partly done | 8 |
| Left out by decision (Ollama-compatible endpoint, Anthropic driver, TLS interception) | 3 |

The two biggest unknowns are how accurate it is on real papers, because every result so far uses
short synthetic ones, and whether it works on Windows, because WSL2 support has never run on real
WSL2.

**Machines in use:** a Debian desktop and a Windows laptop. All the optional features are planned
for use, so all their real-world checks below apply.

## In progress
| Item | State |
|---|---|
| WSL2 support | Committed on the `wsl2` branch, not pushed or merged |

## Decisions and actions for the owner
| # | What | Why it matters |
|---|---|---|
| A1 | Run the WSL2 checklist on the laptop ([guide](wsl2.md)) | The first real evidence for Windows |
| A2 | Choose about 20 research papers on one question: about 14 to keep and 6 to reject. Can be done later | Measures how many relevant papers screening loses |
| A3 | Set up restic to a real remote | The only backup is on the same disk as the data |
| A4 | Keep the backup password somewhere off this machine | Without it, backups can't be restored after a disk failure |
| A5 | Try one real restore | The real repository has never been restored |
| A6 | Decide the Strict overhead target | Strict takes 105 ms with isolation, against a 50 ms target. Recommendation: change the target, not the code |
| A7 | Turn on disk encryption on both machines (LUKS, or BitLocker on Windows) | Protects unlocked conversations if a disk is stolen |
| A8 | Decide whether the repository goes public | Release signing writes its name to a public log, so the workflow stops before signing while it's private |
| A9 | Decide on the injection test corpus. Recommendation: write it | The requirements name it, and it doesn't exist yet |
| A10 | Find an outside reviewer | Only the assistant that wrote the code has reviewed it |

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
| I20 | PaperQA2 can say a source "explicitly states" something it doesn't. The checker only catches this inside quotation marks ([reruns](bench/results-2026-11-04-paperqa-reruns.md)) | Open |

### Functionality
| # | Issue | State |
|---|---|---|
| I9 | Restoring locked job, run and library files is by hand. Only conversations are imported automatically | Open |
| I10 | Scheduled backups and replication stop when WSL goes idle | Accepted: documented |
| I11 | `keyring:` references usually fail under WSL. Use `env:` | Accepted: documented |
| I12 | Only the built-in APA and IEEE citation styles work until pandoc or citeproc-js is installed | Your choice |
| I13 | Audit anchoring is off, so there's no outside proof of the audit log | Your choice |
| I21 | PaperQA2's refusals in other words aren't recognised as refusals, so `cannot_answer` is wrong | Open |
| I22 | The check fails a correct "Six" on q5 because it looks for "6", so `cairnward check` can report a false failure | Open |

### Performance
| # | Issue | State |
|---|---|---|
| I14 | Strict overhead misses its target (A6) | Waiting on a decision |
| I15 | Start time is 2.8 s on empty data, against a 3 s target. A large data folder hasn't been tried | Untested |

### Unexplained or uncovered
| # | Issue | State |
|---|---|---|
| I16 | One slow fast-model run in the first-token measurements | Unexplained |
| I17 | Question q5's failure in the first eval. I22 could be the cause, but those answers weren't kept | Unexplained |
| I18 | `core/server.py` isn't in the coverage count. Only the isolation job runs it | Accepted |
| I19 | CI doesn't run WSL2, so a change that breaks it only shows up on a real machine | Accepted |

## Testing still to do
### Could reveal real bugs
- [x] Full check on `main`, 2026-11-04: overall fail (report on the `dev` branch). Lean passed 8 of
      8, PaperQA2 6 of 8 (I20 to I22), and the coding and review evals passed. Chat clients were
      skipped because none has made a model call yet
- [ ] Real papers: accuracy, and how many relevant papers screening loses
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
- [ ] Injection test corpus (A9)
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
- **PR 18:** home folder hidden from agent runs (merged)
- **PR 17:** replication through a shared folder (merged)
- **PR 16:** default backup folder, and a choice of folder and permissions (merged)
- **PR 15:** research workflows (merged)
- First backup taken on the desktop
- Full check run on `main`, and PaperQA2's two failures rerun
