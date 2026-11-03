# Threat model review, 2026-11-03 (NFR-S2)

The first recorded review, covering everything up to M3 and the follow-ups. Before this,
[threat-model.md](../threat-model.md) was last changed on 2026-11-01. Each claim below was
checked by searching the code and tests, reading the relevant code, or looking at this
machine's files. It wasn't a penetration test, and nothing was attacked. A review by someone
other than the project's own coding assistant is still outstanding.

## Controls the model listed that don't exist

Corrected in the model. They made the document read stronger than the code.

| Where | Claimed | Found |
|---|---|---|
| T-16, LLM03 | Signed releases and SBOM | No release workflow (NFR-SUP-2) |
| LLM01 | Injection test corpus | No corpus. `tests/security/test_user_only_actions.py` tests the control that matters most, but not a set of injection attempts |
| LLM08 | Locked chats' embeddings encrypted; per-chat index scoping | Conversations have no embeddings. Library chunks are plain text in `research.db`, and that's stated in `research/library.py` |
| P-05 | Payload preview on approval | Not found in the approval code. Approvals show the action, target and whether untrusted content arrived |
| LLM05 | Markdown rendered without raw HTML in the chat view | There's no chat view: Cairnward has no chat UI of its own |
| T-05, T-13, LLM06 | Sync event validation, sub-agent limits, `max_parallel` | Replication and multi-agent workflows are deferred (D71) |

## Components the model didn't cover

Added to the model as T-22 to T-27.

| New | Threat | State of the controls | Risk |
|---|---|---|---|
| T-22 | Remote MCP servers | Through egress, with pinning; tested against a local SDK server only. No OAuth yet | Medium |
| T-23 | A steered model puts private text into an academic search query | Opt-in per environment, allow-listed hosts, audited. The query text still leaves the machine, and egress can't read it | Medium |
| T-24 | Research library stored in plain text and can't be locked | Owner-only folder, included in encrypted backups. Locking a conversation doesn't cover a library it used | Medium |
| T-25 | Third-party engines parse untrusted PDFs | PaperQA2 and Docling sandboxed with isolation on; Presidio runs inside the core's sandbox. PaperQA2's cache is a `pickle` file | Low |
| T-26 | Model reasoning delivered to clients as if it were the answer | `medium` now asks for reasoning, so it stays separate. See below | Was high; now low |
| T-27 | Losing the only copy of the backup password | By default it's only in the state folder, so losing that disk makes backups useless. Stated in `backup.py` but not at setup | Medium |

### T-26: the most serious finding

With reasoning "off", `qwen3:30b` still reasons, in the visible reply, ending with a stray
`</think>` (seen calling Ollama directly, so it isn't caused by the gateway). The gateway passes
this to clients unchanged. In the real Aider run through Cairnward:

| Reply | Characters | Before `</think>` | After `</think>` | Edit blocks before `</think>` |
|---|---|---|---|---|
| Second call | 9,209 | 9,029 (98%) | 172 | 3 |
| Final call | 25,663 | 25,583 (99.7%) | 72 | 0 |

So a client is shown drafts the model never settled on as if they were the answer. Aider's edit
parser ran over that text, including edit blocks written while reasoning. That run's test passed,
so no harm was seen, but nothing stops a discarded draft being applied. All of this text is also
kept in the vault. It's also the main cost of the slow coding run (see
[results](../bench/results-2026-11-03-measurements.md)).

## Checked and holding

- **Loopback, Host and Origin checks, CSRF:** `tests/security/test_local_api.py`.
- **User-only actions always ask, even with injected content:** `policy/engine.py`,
  `tests/security/test_user_only_actions.py`.
- **Tool poisoning and rug pulls:** description scan and pinning in `mcp/pins.py`, tested in
  `tests/test_mcp.py`.
- **Locked conversations never go to hosted backends:** `gateway/app.py`, `tests/security/test_lock.py`.
- **OS isolation:** the isolation suite passed on this machine today (13 passed; 3 needed the
  PaperQA2 path and passed separately), and doctor's self-test passes on the running server.
- **File permissions:** the data and state folders are 700 here. Some files inside are 664
  (`audit.jsonl`, `prompt_sizes.json`), and the config folder is 775. Only the parent folder protects
  those files. The config holds no secrets by design, so this is low risk, but tighter modes
  would be better.

## Still open after this review

1. T-26: a custom effort level with thinking off, on a model that ignores it, still leaks.
2. T-27: say at setup that the backup password must be kept somewhere else too.
3. Tighten the file modes above.
4. Write an injection test corpus, or drop the idea on purpose.
5. Review by someone other than the assistant that wrote the code.

Reviews are now prompted by the pull request template.
