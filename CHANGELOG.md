# Changelog

## Unreleased
- **Reasoning in streamed answers (I5):** a model's first streamed reply with thinking off no
  longer reaches the client with its reasoning in the answer. The answer is held until it's
  clear, and the reasoning goes out as `reasoning_content`. That one reply per model arrives all
  at once; after it, a clean model streams word by word as before.
- **Plain HTTP through egress (I28):** a keep-alive client's second `http://` request no longer
  fails. Egress now says `Connection: close`. A request pipelined after the first is no longer
  passed to the host unchecked. A request with two `Host` headers gets 400.
- **TLS inspection fixes** (threat-model review of 2026-11-05):
  - Only `cairnward run` agents are decrypted. MCP servers, remote restic backups, `mcp login`,
    plugin commands and downloads would otherwise have failed TLS in an inspecting environment.
  - A request naming another site than its tunnel is refused (domain fronting).
- **TLS inspection** ([ADR-0018](docs/adr/0018-openshell-sandbox-and-tls-inspection.md)): per
  environment, off by default. With `[tls_inspection] enabled = true`, cairnward-egress decrypts
  runs' HTTPS with a local CA, checks each request against method and path rules, and audits it
  (the path only as a digest). `cairnward egress-ca show|rotate` manage the CA.
- **OpenShell as a second sandbox** ([ADR-0018](docs/adr/0018-openshell-sandbox-and-tls-inspection.md)):
  `sandbox = "openshell"` runs agents in an OpenShell sandbox that can reach only Cairnward. The
  agent works on a copy of the workspace, copied back at the end. Not yet run with a real OpenShell.
- **The core no longer ships `litreview-offline` and `litreview-online`.** Plugins bring their own environments. Run `cairnward-research install-plugin` again to create them. `citation_style` is ignored if set.
- **Research moved to a plugin** ([ADR-0017](docs/adr/0017-research-as-a-plugin.md)). `cairnward
  library`, `research`, `jobs` and `cite` are now `cairnward-research ...`, from its own package. Run
  `cairnward-research import` once. `[research]` in `cairnward.toml` and `research_sources` are
  ignored: set them in the plugin's `research.toml`.
- **Plugin contract:** plugins run sandboxed with no network of their own, using the core's services
  for models, outside requests, saved output and keys. See [docs/plugins.md](docs/plugins.md).
- **Prompt injection:** research prompts now say a paper's text isn't instructions, and an injection
  corpus runs on the real model. One planted instruction still gets through sometimes.
- **Strict overhead target** raised from 50 ms to 75 ms, or 150 ms with isolation. Nothing got
  faster: the target was changed by decision, and the numbers are the same.
- **Fixed, on isolated servers:** changing settings from the dashboard failed with a 500, and a locked
  conversation's artifacts couldn't be read after unlocking. Neither had ever been run for real.
- **`cairnward backup restore`** now brings back artifacts, jobs and plugins' data too, not just
  conversations.
- **Backups** hold plugins' data under `plugins/<name>/`, without installed engines.
  `include_research` is now `include_plugins`.
- **WSL2 support** (not yet run on real WSL2), and a warning for backends on the network that use
  plain `http://`.
