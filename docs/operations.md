# Running Cairnward

## Start it with your session
There's a user service in [`deploy/systemd/cairnward.service`](../deploy/systemd/cairnward.service). It hasn't been run under systemd for real yet: its syntax is only checked by `systemd-analyze verify`, when available.

Check on it:

```bash
cairnward status
```

```bash
cairnward doctor
```

## Upgrade
1. Take a backup: `cairnward backup run`.
2. Update the code and restart `cairnward serve`. Databases upgrade themselves when opened. Going back to an older version after that isn't supported, so restore the backup instead.
3. If a plugin was updated, its pinned code no longer matches, and it won't start until you review it:

```bash
cairnward plugin pin research
```

```bash
cairnward plugin verify research
```

Upgrades have only been tried by the tests' schema migrations, not from one real release to the next, because there's been no release yet.

## Back up and restore
See [Getting started](getting-started.md#8-back-up). What to keep off this machine is the backup password: without it, a backup can't be opened after a disk failure. **Try a restore before relying on it.**

`cairnward backup restore` brings back conversations, artifacts, jobs and plugins' data, and locked ones stay locked. Each item comes back whole or not at all: anything already here is left as it is, and a purged conversation's artifacts stay gone. Stop the server first if a plugin's data is coming back. `--to <folder>` only extracts the backup, if you'd rather look first. Audit records, settings and the plugin's installed engines aren't brought back: they're in the extracted copy, apart from engines, which you install again.

## Remove it
1. Stop the server, and disable the service if you installed it.
2. Delete `~/.config/cairnward`, `~/.local/share/cairnward` and `~/.local/state/cairnward`. **This deletes your conversations, audit log and keys for good.**
3. Delete `~/cairnward-backups`, or wherever your backups are, if you don't want them.
4. Delete `~/.cache/cairnward` if it's there.
5. Remove the program: `uv tool uninstall cairnward`.

## When something's wrong
| Symptom | Look at |
|---|---|
| A check fails | `cairnward doctor`, which gives the fix |
| An MCP server or plugin won't start | `~/.local/state/cairnward/logs/mcp-<name>-<environment>.log` |
| A plugin says its code changed | Review the update, then `cairnward plugin pin <name>` |
| A request waits for ever | `cairnward approvals`: it may be waiting for you |
| The audit log looks wrong | `cairnward audit verify`, which says where the chain breaks |

There's no telemetry, and no one to send logs to: see [SECURITY.md](../SECURITY.md) for reporting a security problem.
