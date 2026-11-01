# T0.6 sandbox spike

Proves that Anthropic's [sandbox-runtime](https://github.com/anthropics/sandbox-runtime) (`srt`) gives Cairnward the isolation it needs on Linux. Decision and findings: [ADR-0013](../../docs/adr/0013-reuse-sandbox-runtime-for-isolation.md).

## What it checks
- `run_spike.sh`: runs `probe.py` inside srt and checks 13 behaviours: the gateway on the host's loopback is reachable through the proxy; direct network, DNS, non-allowed domains, a secret directory, writes outside the workspace, git hooks/config, Unix sockets and nested user namespaces are all blocked.
- `run_chain.sh`: srt forwards allowed traffic to an upstream proxy (`fake_egress.py`, a stand-in for `cairnward-egress`) that logs and makes the final decision; hosts srt itself refuses never reach it.

## Run it (Debian/Ubuntu, as a normal user)
Prerequisites: bubblewrap, socat, ripgrep, Node.js ≥ 22.12, Python 3.

```bash
sudo apt install bubblewrap socat ripgrep
```
Debian 13 ships Node 20; install Node 22, e.g. with [fnm](https://github.com/Schniz/fnm) or NodeSource packages. Then:
```bash
cd spikes/t06-sandbox && npm ci
```
```bash
./run_spike.sh
```
```bash
./run_chain.sh
```
Expected: `13/13 checks behaved as required` and `chain: all checks behaved as required`.

On Ubuntu 24.04+ unprivileged user namespaces are restricted by AppArmor; allow them with `sudo sysctl -w kernel.apparmor_restrict_unprivileged_userns=0` (CI does this).

## Notes
- srt sets `no_proxy=localhost,127.0.0.1` inside the sandbox; the scripts clear it so the gateway is reached via the proxy. `cairnward run` will do the same.
- srt rejects a catch-all allow-list (`*`), so Cairnward generates the allow-list from its policy and updates it live (`--control-fd`).
- This is spike code: it will be replaced by `cairnward run` (T0.12) and kept as a regression test until then.
