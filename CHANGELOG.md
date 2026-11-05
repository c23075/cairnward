# Changelog

## Unreleased
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
