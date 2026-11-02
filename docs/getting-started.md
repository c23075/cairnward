# Getting started (Debian/Ubuntu)

## 1. Install
```bash
sudo apt install git bubblewrap socat ripgrep
```
Node.js 22.12+ is needed for the sandbox (Debian 13 ships Node 20), e.g. with fnm:
```bash
curl -fsSL https://fnm.vercel.app/install | bash
```
```bash
source ~/.bashrc && fnm install 22 && fnm use 22
```
Then Cairnward itself (from the repository):
```bash
git clone https://github.com/c23075/possessed-incantation.git ~/projects/cairnward
```
```bash
cd ~/projects/cairnward && uv sync
```

## 2. Choose a privacy preset and check everything
```bash
uv run cairnward setup --install-sandbox
```
`setup` asks for Open / Balanced / Strict, installs the pinned sandbox if the preset needs it, records the settings in the audit log and runs `cairnward doctor`. Re-run `uv run cairnward doctor` any time.

## 3. Start Cairnward
```bash
uv run cairnward serve
```
With isolation on, this runs the core inside the sandbox and `cairnward-egress` as the only networked process. Check with:
```bash
uv run cairnward status
```

## 4. Connect clients and agents
- Chat apps: [Open WebUI](clients/open-webui.md), [Jan](clients/jan.md), [LibreChat](clients/librechat.md); server-style apps can run [with no network of their own](clients/containers.md).
- MCP tools for any client or agent: [MCP through Cairnward](clients/mcp.md).
- Coding agents: [Aider](clients/aider.md) (recommended on CPU), [Qwen Code](clients/qwen-code.md), [opencode](clients/opencode.md), [Goose](clients/goose.md) (untested), or any OpenAI-compatible agent with `--profile generic`.

Every `cairnward run` takes snapshots of the project before and after, so you can review or undo what the agent did:
```bash
uv run cairnward runs list
```
```bash
uv run cairnward runs diff <run-id>
```
```bash
uv run cairnward runs revert <run-id>
```
With `network = "ask"` (the Balanced preset), a sandboxed agent that contacts a host not on your list waits while Cairnward asks you. Answer in a second terminal (a desktop notification appears if `notify-send` is installed; the serve terminal also prints a line):
```bash
uv run cairnward approvals
```
`y` allows that host for the rest of the run, `c` for the conversation, `e` for the environment (until Cairnward restarts), `n` refuses. Nobody answering within `approval_timeout_s` (default 120 s, `[egress]` in `cairnward.toml`) counts as no. Addresses of this machine (`localhost`, `127.x`, link-local) are refused without asking unless listed exactly in `network_allow_hosts`.

Stop runaway sessions with `--max-requests`, `--max-tokens` or `--max-minutes` on `cairnward run` (or `[run_budget]` in an environment).

Let Cairnward warn you about slow prompts by importing your benchmark results:
```bash
uv run cairnward bench import bench-qwen3_30b-*.json bench-qwen3_1.7b-*.json
```

Download model files through Cairnward, verified against the publisher's SHA-256 (GGUF or safetensors only; Hugging Face downloads also need its CDN hosts allowed):
```bash
uv run cairnward models pull https://huggingface.co/<org>/<repo>/resolve/main/<file>.gguf --sha256 <hash>
```

## 5. See what happened
Open the dashboard (approvals, activity, audit, egress, vault, MCP tools, settings) with a one-time sign-in link:
```bash
uv run cairnward dashboard
```
```bash
uv run cairnward audit log --last 30
```
```bash
uv run cairnward audit verify
```
```bash
uv run cairnward vault list
```
```bash
uv run cairnward vault search "parser"
```
`vault show | export | delete | restore | purge` take an id or its first characters. Deleting here doesn't touch the client's own history. Lock a conversation with `vault lock <id>` (passphrase; a locked conversation never goes to hosted models) and open it for a while with `vault unlock <id>`.
```bash
uv run cairnward store info
```
What left this machine, per destination (connections, bytes, denials, hosted model tokens, redaction flags):
```bash
uv run cairnward audit egress --since 24h
```
Filter with `--run`, `--client`, `--env`, `--conversation`; export with `--json` or `--csv`. `audit log` takes the same filters.

Anchor the audit log to a private git repo you own, so later edits on this machine are detectable (on in Strict; follow the printed steps):
```bash
uv run cairnward audit anchor --setup
```
```bash
uv run cairnward audit verify --anchors
```

## 6. A literature library
Pull the embedding model once:
```bash
ollama pull qwen3-embedding:0.6b
```
Register a folder of PDFs and index it (re-run `index` after adding papers; only new or changed files are processed):
```bash
uv run cairnward library create thesis ~/research/papers
```
```bash
uv run cairnward library index thesis
```
Find passages, with pages:
```bash
uv run cairnward library search thesis "effect of sleep on working memory"
```
Ask a question; the answer cites passages as [n] with file and page, and says so when the papers don't answer it (a few minutes on a CPU; through the running server when there is one):
```bash
uv run cairnward library ask thesis "Does longer sleep improve working memory?"
```
Check a draft's quotations and references against the library:
```bash
uv run cairnward library check thesis draft.md
```
Format references exported from Zotero as CSL-JSON (APA 7 or IEEE):
```bash
uv run cairnward cite references.json --style apa
```
For output identical to Zotero's, and any of the 10,000+ CSL styles (`--style path/to/style.csl`), install the citeproc-js engine (needs Node.js 20 or newer; pinned and hash-checked), or have pandoc installed; Cairnward then uses it automatically (`[research] citation_engine = "auto"`):
```bash
uv run cairnward research engine install citeproc-js
```
Run a review overnight: notes per paper and a comparison table with references (it picks up where it left off if Cairnward restarts):
```bash
uv run cairnward research review thesis --question "Does sleep affect working memory?" --at 23:30
```
The papers are picked when the review starts, so PDFs you add to the library folder before then are indexed and included. To review only some papers (paths inside the library folder; they can be added after queuing):
```bash
uv run cairnward research review thesis --question "Does sleep affect working memory?" --files new/smith2024.pdf new/lee2025.pdf --at 23:30
```
```bash
uv run cairnward jobs list
```
Optional deeper engine, PaperQA2 (more model calls per question, so several minutes more on a CPU; runs sandboxed, every call through Cairnward):
```bash
uv run cairnward research engine install paperqa
```
```bash
uv run cairnward library ask thesis "Does longer sleep improve working memory?" --engine paperqa
```
Or overnight, for a list of questions:
```bash
uv run cairnward research ask thesis questions.txt --engine paperqa --at 23:30
```
Chat clients can do the same through MCP: add `tools = ["research"]` to the library's environment for `cairnward__lit_search` and `cairnward__lit_ask`.

## 7. Back up
Backups use [restic](https://restic.net) (`sudo apt install restic`), always encrypted. Add to `cairnward.toml`, with a folder on another disk:
```toml
[backup]
mode = "auto"            # or "on_demand"
repository = "/media/backup/cairnward"
```
Then create the repository (keep the password it shows somewhere safe) and take the first backup:
```bash
uv run cairnward backup init
```
```bash
uv run cairnward backup run
```
With `serve` running, `auto` backs up every 24 hours. Purging a conversation also removes it from every earlier backup. To bring conversations back (only ones the vault doesn't have):
```bash
uv run cairnward backup restore --at 2026-11-01
```

## 8. Check everything on your machine
With `cairnward serve` running and your models pulled, one command runs the doctor, the lit.ask eval (and PaperQA2 next to it when installed), the coding eval with each installed agent, and summarises what your chat clients did:
```bash
uv run cairnward check
```
It writes `docs/bench/check-<date>.md` (and a `.json` next to it) with no prompts or answers in it. On a CPU the evals take a while (roughly 10–40 minutes); `--no-paperqa` and `--no-coding` make it shorter. Fill in the manual client checklist at the end of the report after trying each chat app.

## Important: Ollama can still be reached directly
Ollama listens on `127.0.0.1:11434`. Agents started with `cairnward run` (sandboxed) can only reach Cairnward's gateway, but **any unsandboxed program on your machine can talk to Ollama directly and bypass Cairnward**. Configure chat apps to use Cairnward's gateway, not Ollama, and turn off their own Ollama connection (see the client guides).
