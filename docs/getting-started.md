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
- Chat apps: [Open WebUI](clients/open-webui.md), [Jan](clients/jan.md).
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

## 5. See what happened
```bash
uv run cairnward audit log --last 30
```
```bash
uv run cairnward audit verify
```
```bash
uv run cairnward store info
```
What left this machine, per destination (connections, bytes, denials, hosted model tokens, redaction flags):
```bash
uv run cairnward audit egress --since 24h
```
Filter with `--run`, `--client`, `--env`, `--conversation`; export with `--json` or `--csv`. `audit log` takes the same filters.

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

## Important: Ollama can still be reached directly
Ollama listens on `127.0.0.1:11434`. Agents started with `cairnward run` (sandboxed) can only reach Cairnward's gateway, but **any unsandboxed program on your machine can talk to Ollama directly and bypass Cairnward**. Configure chat apps to use Cairnward's gateway, not Ollama, and turn off their own Ollama connection (see the client guides).
