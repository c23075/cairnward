# Getting started (Debian/Ubuntu)

On Windows, read [Running Cairnward on Windows (WSL2)](wsl2.md) first, then follow this inside WSL.

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
`vault show | export | delete | restore | purge` take an id or its first characters. Deleting here doesn't touch the client's own history. Lock a conversation with `vault lock <id>` (passphrase; a locked conversation never goes to hosted models) and open it for a while with `vault unlock <id>`. A forgotten passphrase can't be recovered unless you set up a recovery key first (`vault recovery-key create`; keep what it shows off this computer); then `vault recover <id>` sets a new passphrase.
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

## 6. Plugins (optional)
A plugin adds tools to Cairnward and runs under its protections. See [Plugins](plugins.md). If you used `cairnward library` before research moved out, [`literature-review-agent-plugin`](https://github.com/c23075/literature-review-agent-plugin) has it now, and its README says how to bring your libraries over.

## 7. Hosted models (optional)
Cairnward runs local models by default; hosted providers are off until you add one. List the ready-made templates and print one to paste into `backends.toml`:
```bash
uv run cairnward backend providers
```
```bash
uv run cairnward backend template openrouter-free
```
Keys come from environment variables (e.g. `export OPENROUTER_API_KEY=...`), never from config files. Hosted backends are still refused until an environment allows them (`hosted_backends = "ask"` or `"allow"`), never get locked conversations, and free tiers that may train on your prompts only answer in environments marked `scratch = true`.

## 8. Back up
Backups use [restic](https://restic.net) (`sudo apt install restic`), always encrypted. They hold your chats, artifacts, plugins' data, audit log and settings. To turn them on:
```bash
uv run cairnward backup use /media/backup/cairnward
```
With no folder they go to `~/cairnward-backups`, but that's on the same disk as your data, so it protects against mistakes and not a failed disk. `--mode on_demand` backs up only when asked. By default only you can read the folder; `--access group` also lets your group read it (for example so another account can copy it). The contents stay encrypted either way. The same settings are in `[backup]` in `cairnward.toml` and in the dashboard's Backups view, and `include_research`, `include_artifacts` and `include_config` choose what's included.
A remote repository works too (`s3:`, `rest:`, `b2:`, `azure:`, `gs:`, `swift:` or `rclone:`, e.g. `repository = "s3:s3.eu-central-1.amazonaws.com/my-bucket/cairnward"`, with the provider's keys in restic's usual environment variables). Its traffic goes through Cairnward's egress, so `serve` must be running and the backup environment (`[backup] environment`, default the default one) must allow the host, e.g. by adding it to `network_allow_hosts`.

That also creates the repository, and shows its password once: keep a copy somewhere else. Then take the first backup:
```bash
uv run cairnward backup run
```
With `serve` running, `auto` backs up every 24 hours. Purging a conversation also removes it from every earlier backup. To bring conversations back (only ones the vault doesn't have):
```bash
uv run cairnward backup restore --at 2026-11-01
```

## 9. The same chats on another device (optional)
Use any folder both devices can reach: a Syncthing folder, a NAS mount or a synced drive. Cairnward only reads and writes files there, and everything it writes is encrypted. On the first device:

```bash
uv run cairnward replicate init /path/to/shared/folder
```

This shows a sync key, once. Keep it like a password: it opens the folder. **Every conversation on the device is published at the next sync**, so list any environment that should stay put in `[replication] exclude_environments` first. On the other device, when the folder has arrived:

```bash
uv run cairnward replicate join /path/to/shared/folder
```

Back on the first device, approve it (`status` lists devices), then restart `serve` on both to start syncing every 5 minutes. `cairnward replicate sync` does it right away.

```bash
uv run cairnward replicate approve <id>
```

If both devices reply in the same conversation before syncing, both replies are kept, as a fork. Purging a conversation purges it everywhere, once each device has synced. Turn off version history for this folder in Syncthing or your cloud drive, or old files outlive purges and locks. Research libraries and settings aren't shared. If you lose a device, `replicate block <id>` stops its changes being accepted, but it can still read the folder with the key it holds: to shut it out, run `replicate leave` then `init` on a new folder, and `join` from the others.

## 10. Check everything on your machine
With `cairnward serve` running and your models pulled, one command runs the doctor and the coding eval:
```bash
uv run cairnward check
```
It writes `docs/bench/check-<date>.md` (and a `.json` next to it) with no prompts or answers in it. On a CPU the evals take a while (roughly 10–40 minutes); `--no-paperqa` and `--no-coding` make it shorter. Fill in the manual client checklist at the end of the report after trying each chat app.

## Important: Ollama can still be reached directly
Ollama listens on `127.0.0.1:11434`. Agents started with `cairnward run` (sandboxed) can only reach Cairnward's gateway, but **any unsandboxed program on your machine can talk to Ollama directly and bypass Cairnward**. Configure chat apps to use Cairnward's gateway, not Ollama, and turn off their own Ollama connection (see the client guides).
