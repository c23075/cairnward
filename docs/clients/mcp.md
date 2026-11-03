# MCP tools through Cairnward

Cairnward offers the MCP servers enabled for an environment as one MCP endpoint. Every tool call is checked against your privacy settings, may wait for your approval, and is recorded in the audit log (arguments and results as digests only). Third-party servers run in their own sandbox with only the permissions you declare.

## 1. Declare a server (`~/.config/cairnward/mcp.toml`)
```toml
[servers.notes]
description = "Notes in the project"
command = ["npx", "-y", "@example/notes-mcp@1.4.2"]   # pin the version you reviewed
version = "1.4.2"
cwd = "{workspace}"

[servers.notes.permissions]
network = ["registry.npmjs.org"]   # hosts it may reach (through egress)
write = ["{workspace}"]            # inside the workspace or {data} (its own folder and HOME)
# read = ["{workspace}"]           # only these (plus system folders); default: all but secrets
env = []                           # your environment variables it may see

[servers.notes.tools]
delete_note = "ask"                # allow / ask / deny per tool
```
For a local script you can also pin its file: `pin_file = "~/mcp/server.py"` and `sha256 = "<hash>"`; a changed file stops the server from starting.

Enable it in an environment (`environments/coding.toml`):
```toml
mcp_servers = ["notes"]
```

## 2. Review and approve its tools
With `mcp_pinning = "on"` (Balanced and Strict), tools are offered only after you approve their exact definitions:
```bash
uv run cairnward mcp list
```
```bash
uv run cairnward mcp tools notes
```
```bash
uv run cairnward mcp approve notes
```
Warnings such as "hidden-instruction tag" or "asks for secrecy" mean the description contains instruction-like text (tool poisoning): don't approve it unless you understand why. If a server later changes a definition, that tool disappears until you approve it again.

## 3. Connect a client
- **Streamable HTTP:** `http://127.0.0.1:8484/mcp/<environment>` with header `Authorization: Bearer <key from cairnward client add>`.
- **stdio** (clients that start a command): command `cairnward mcp --env <environment>` with `CAIRNWARD_API_KEY=<key>` in its environment.
- **`cairnward run`:** agents get `CAIRNWARD_MCP_URL` and `CAIRNWARD_API_KEY` for the run; opencode's per-run config includes the endpoint automatically. The run's environment decides which tools it sees.

Tools appear as `<server>__<tool>`, e.g. `notes__delete_note`. Server logs: `logs/mcp-<server>-<environment>.log` in the state folder.

## Cairnward's own tools and skills
- `tools = ["research"]` in an environment adds `cairnward__lit_search`: passages from that environment's literature libraries (`cairnward library ...`), with titles and page numbers.
- `tools = ["vault"]` adds `cairnward__lock_conversation`, which you confirm (and give the passphrase) in Cairnward, never in the chat.
- Skills: put a folder with a `SKILL.md` under `~/.config/cairnward/skills/<name>/` and list it in the environment's `skills = [...]`. Clients that support MCP prompts show it as a prompt; its other files are MCP resources.

## Remote servers that need a sign-in

For a remote server that uses OAuth, set this in `mcp.toml`:

```toml
[servers.notes]
url = "https://mcp.example.org/mcp"
auth = "oauth"
oauth_scope = "notes"  # optional
```

Add it to an environment's `mcp_servers`, allow its host and the sign-in server's host in that environment's network rules, then sign in once with the server running:

```bash
uv run cairnward mcp login notes
```

Your browser opens the sign-in page (`--no-browser` only prints the address). Tokens are stored owner-only in the state folder, and Cairnward refreshes them by itself. If a refresh is refused, the server's tools report that you need to sign in again. To forget them:

```bash
uv run cairnward mcp logout notes
```

Tested against a fake authorization server only, with the real MCP SDK; no real provider has been tried.
