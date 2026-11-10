# Changelog

## Unreleased

### Added
- Merkle checkpoints (D79): each audit anchor also signs the RFC 9162 Merkle root over every
  record so far, `audit verify --anchors` checks each root against the log and that each
  checkpoint extends the last, and `audit prove` / `audit check-proof` show one record is in a
  signed checkpoint without the rest of the log. Older checkpoints still verify.
- An optional `sqlcipher` extra (Linux x86-64) and a storage layer that opens and converts
  SQLCipher databases (ADR-0019, proposed). Not yet a setting: nothing is encrypted this way yet.

### Security
- TLS inspection: method and path rules now match the normalized path (escaped unreserved
  characters decoded, `//`, `.` and `..` resolved), and a path with an encoded slash, backslash
  or NUL is refused. Each request must also be allowed when its path is read without `;`
  parameters (Tomcat reads `/a/..;/b` as `/b`) and ignoring case. Before, `/repos/me/../../other` passed an allow rule for `/repos/me/**`, and
  `//admin` or `/%61dmin` slipped past a deny rule for `/admin`. Found in an audit, tested
  against the rule checker only.

### Fixed
- Host patterns (`network_allow_hosts`, `skip_hosts`, inspection rules) now match IPv6
  addresses written without a port, with or without brackets (`::1`, `[::1]`); before, only
  `[::1]:443` matched, so such an entry was ignored and the connection denied or asked about.

## 0.1.0 (2026-11-06)

The first tagged release. Cairnward is a local privacy gateway for AI chat clients and coding
agents on one person's machine. It has a model gateway, an egress proxy, a policy engine with
approvals, a hash-chained audit log, an encrypted transcript vault, an MCP proxy and a plugin
contract. See the [README](README.md) and [docs/spec.md](docs/spec.md).

**Not production ready.** What it hasn't had yet ([docs/status.md](docs/status.md)):
- an outside security review;
- a run on real WSL2;
- a restore from a real backup;
- tests of several features against real services, not stand-ins: hosted model providers,
  remote MCP servers, OpenShell, and TLS inspection with real agents.

What its protections don't do is in [docs/limitations.md](docs/limitations.md).

Notes before this release weren't kept per version. The entries below cover the last stretch
of work, from the research plugin split onwards.

### If you ran an earlier checkout
- **The core no longer ships `litreview-offline` and `litreview-online`.** Plugins bring their own environments. Run `cairnward-research install-plugin` again to create them. `citation_style` is ignored if set.
- **Research moved to a plugin** ([ADR-0017](docs/adr/0017-research-as-a-plugin.md)). `cairnward
  library`, `research`, `jobs` and `cite` are now `cairnward-research ...`, from its own package. Run
  `cairnward-research import` once. `[research]` in `cairnward.toml` and `research_sources` are
  ignored: set them in the plugin's `research.toml`.
- **Backups** hold plugins' data under `plugins/<name>/`, without installed engines.
  `include_research` is now `include_plugins`.
- **Strict overhead target** raised from 50 ms to 75 ms, or 150 ms with isolation. Nothing got
  faster: the target was changed by decision, and the numbers are the same.

### New
- **TLS inspection** ([ADR-0018](docs/adr/0018-openshell-sandbox-and-tls-inspection.md)): per
  environment, off by default. With `[tls_inspection] enabled = true`, cairnward-egress decrypts
  runs' HTTPS with a local CA, checks each request against method and path rules, and audits it
  (the path only as a digest). `cairnward egress-ca show|rotate` manage the CA.
- **OpenShell as a second sandbox** ([ADR-0018](docs/adr/0018-openshell-sandbox-and-tls-inspection.md)):
  `sandbox = "openshell"` runs agents in an OpenShell sandbox that can reach only Cairnward. The
  agent works on a copy of the workspace, copied back at the end. Not yet run with a real OpenShell.
- **Plugin contract:** plugins run sandboxed with no network of their own, using the core's services
  for models, outside requests, saved output and keys. See [docs/plugins.md](docs/plugins.md).
- **`cairnward backup restore`** now brings back artifacts, jobs and plugins' data too, not just
  conversations.
- **WSL2 support** (not yet run on real WSL2), and a warning for backends on the network that use
  plain `http://`.

### Security and privacy fixes
- **VM sockets refused in the isolated core (I1):** the core's sandbox allows Unix sockets, which
  turned off srt's VM-socket block there. Under WSL2 that could reach Windows past egress. The core
  now refuses VM sockets (and io_uring) itself, won't start under WSL2 without that, and `doctor`
  checks it.
- **Plain HTTP through egress (I28):** a keep-alive client's second `http://` request no longer
  fails. Egress now says `Connection: close`. A request pipelined after the first is no longer
  passed to the host unchecked. A request with two `Host` headers gets 400.
- **TLS inspection fixes** (threat-model review of 2026-11-05):
  - Only `cairnward run` agents are decrypted. MCP servers, remote restic backups, `mcp login`,
    plugin commands and downloads would otherwise have failed TLS in an inspecting environment.
  - A request naming another site than its tunnel is refused (domain fronting).
- **Reasoning in streamed answers (I5):** a model's first streamed reply with thinking off no
  longer reaches the client with its reasoning in the answer. The answer is held until it's
  clear, and the reasoning goes out as `reasoning_content`. That one reply per model arrives all
  at once; after it, a clean model streams word by word as before.
- **Prompt injection:** research prompts now say a paper's text isn't instructions, and an injection
  corpus runs on the real model. One planted instruction still gets through sometimes.

### Other fixes
- **Dashboard backup view (#36):** an unsupported repository such as `sftp:` no longer makes the view fail. It now says why the repository can't be used.
- **Fixed, on isolated servers:** changing settings from the dashboard failed with a 500, and a locked
  conversation's artifacts couldn't be read after unlocking. Neither had ever been run for real.
- **Faster start (I15):**
  - The isolated core no longer imports the MCP SDK (about half a second) until an MCP server or
    the `/mcp` endpoint is first used.
  - Databases already at the newest migration skip loading alembic.
  - The VM-socket filter no longer runs `ldconfig`.
  - About 0.5 to 0.9 s faster to `/health` on the machine measured. `scripts/bench_startup.py`
    measures it.
