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
- Coding agents: [Aider](clients/aider.md) (recommended on CPU), or any OpenAI-compatible agent with `--profile generic`.

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

## Important: Ollama can still be reached directly
Ollama listens on `127.0.0.1:11434`. Agents started with `cairnward run` (sandboxed) can only reach Cairnward's gateway, but **any unsandboxed program on your machine can talk to Ollama directly and bypass Cairnward**. Configure chat apps to use Cairnward's gateway, not Ollama, and turn off their own Ollama connection (see the client guides).
