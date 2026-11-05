# Plugins

A plugin is an MCP server that runs under Cairnward's plugin contract. It has no network of its own
and goes to Cairnward for models, outside requests and saved output, so no protection depends on the
plugin behaving well. The design is in [ADR-0017](adr/0017-research-as-a-plugin.md).

## Install one
A plugin ships a manifest. The research plugin writes its own. It needs `cairnward` on your `PATH`, which `uv run` doesn't do, so first run `uv tool install .` in your Cairnward folder. Then:

```bash
uv tool install ./literature-review-agent-plugin
```

```bash
cairnward-research install-plugin
```

You'll see what it may read, write and reach before anything is added. Then start or restart `cairnward serve` and run:

```bash
cairnward plugin verify research
```

```bash
cairnward mcp approve research
```

`verify` runs the contract's tests against the real sandbox. A plugin that hasn't passed for its current code doesn't start. `approve` shows its tools for review, as for any MCP server.

## Commands
| Command | What it does |
|---|---|
| `cairnward plugin list` | Plugins, and why any can't start |
| `cairnward plugin pin <name>` | Accept its installed code after an update; run `verify` next |
| `cairnward plugin run <name> -- <command>` | Run a command in its sandbox, with your terminal |
| `cairnward plugin allow-read <name> <folder>` | Let it read a folder. A running server sees this after a restart |
| `cairnward plugin remove <name>` | Take it out of `mcp.toml`. Its data stays |

## Write one
Here's a manifest, as TOML:

```toml
name = "demo"
version = "1.0"
command = ["/path/to/venv/bin/demo-server"]
pin_dir = "/path/to/venv/lib/python3.12/site-packages"
fetch_hosts = ["api.example.org"]
environments = ["coding"]

[permissions]
read = []
```

Your server is started with these environment variables:

| Variable | What it is |
|---|---|
| `CAIRNWARD_GATEWAY_URL` | The gateway, such as `http://127.0.0.1:8484/v1` |
| `CAIRNWARD_PLUGIN_KEY` | Your key: a bearer token for the gateway and services |
| `CAIRNWARD_ENVIRONMENT` | The one environment your key works in |
| `CAIRNWARD_CONTRACT` | The core's contract version |
| `CAIRNWARD_PLUGIN_DATA` | Your folder, the only one you can write to |

Keep the proxy settings the sandbox gives you: they're the only route to the gateway.

| Service | Use |
|---|---|
| `/chat/completions`, `/embeddings` | Models, as for any client |
| `POST /cairnward/fetch` | An outside request: `{method, url, headers, body_b64, timeout_s}` returns `{status, headers, body_b64}`. Redirects come back to you |
| `POST /cairnward/artifacts` | Save `{path, content_b64, message}` in your own area |
| `GET /cairnward/artifacts`, `GET /cairnward/artifacts/content?path=` | List and read them |
| `POST /cairnward/item-keys` | A key kept with the conversations' keys: `{op, item, secret}` |
| `GET /cairnward/model-info?model=` | Context size, reasoning, and sub-agent limits |

Run the suite in your own CI with `cairnward plugin verify <name>`, against the core version you declare in `contract_min` and `contract_max`.

## Exceptions
Each is shown by `doctor` and the dashboard, and recorded in the audit log:

- `permissions.network`: exact local addresses only, such as Zotero's `127.0.0.1:23119`.
- `unsandboxed = true`: runs outside the sandbox with unpinned code.
- `unpinned_tools = true`: changed tools aren't held for review.

## What it can't protect
- **Data sent to a listed host.** Redaction only catches what it recognises.
- **Injection.** Actions on other servers after a plugin's output need approval, but a model can still be misled inside the plugin's own tools.
- **Correctness.** Whether a plugin's output is right is the plugin's job.
- **Use without Cairnward.** None of this applies.
