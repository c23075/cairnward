# Changelog

## Unreleased
- **Research moved to a plugin** ([ADR-0017](docs/adr/0017-research-as-a-plugin.md)). `cairnward
  library`, `research`, `jobs` and `cite` are now `cairnward-research ...`, from its own package. Run
  `cairnward-research import` once. `[research]` in `cairnward.toml` and `research_sources` are
  ignored: set them in the plugin's `research.toml`.
- **Plugin contract:** plugins run sandboxed with no network of their own, using the core's services
  for models, outside requests, saved output and keys. See [docs/plugins.md](docs/plugins.md).
- **Backups** hold plugins' data under `plugins/<name>/`, without installed engines.
  `include_research` is now `include_plugins`.
- **WSL2 support** (not yet run on real WSL2), and a warning for backends on the network that use
  plain `http://`.
