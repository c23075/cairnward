# Threat model review, 2026-11-10 (NFR-S2)

Covers database encryption (D80, [ADR-0019](../adr/0019-sqlcipher-encrypt-everything.md)) and
Merkle checkpoints (D79), both built in PR 49.
- **Database encryption** adds a control at rest and a new secret, the database key, with a new
  flow: the outer `serve` process hands it to the sandboxed core.
- **Merkle checkpoints** strengthen an existing control (T-03) and need no new boundary.

**How it was reviewed:**
- The key's path was traced through the code: where it's made, wrapped, read, cached, handed
  over and used, and every process that could see it.
- Findings were tested with the **real sandbox** (sandbox-runtime, bubblewrap) and the real
  SQLCipher driver, with `serve` in isolated and single-process modes
  (`tests/isolation/test_encrypted_storage.py`).
- Each fixed finding has a test that failed before its fix.
- It wasn't a penetration test. The reviewer is the coding assistant that wrote the code, so an
  outside review is still outstanding (A10).

## What changed in the model
- **A4 (credentials):** now includes the database key and its passphrase.
- **New threat T-38:** the database key exposed or lost.
- **T-03:** updated for Merkle checkpoints (done in D79).
- **Accepted risks:** updated for database encryption.

## Findings

| # | Finding | Severity | State |
|---|---|---|---|
| R1 | **The key was readable in the sandboxed core's `/proc/<pid>/environ`.** `serve` handed it over in an environment variable that the core removed from `os.environ`, but removing a variable doesn't erase the process's initial environment block. Any process running as the user that can see the core's pid could read it, and so could the sandbox's wrapper processes | High (it defeats the encryption for anyone on the machine as the user) | **Fixed.** The key goes to the core on standard input, a pipe read once at start (`dbkey.receive`), and the environment only carries a flag. Found by `test_serve_with_encrypted_databases[isolated]`, which read the key from `/proc` before the fix and finds no key in any core or wrapper process after it |
| R2 | **An `env:` key reference was inherited by child processes.** With `[storage] key = "env:NAME"`, the variable stayed in the process environment, and some children are started with the whole environment (`cairnward plugin … -- <command>`) | Medium (the reference is already a documented weakening) | **Fixed.** The variable is removed once read, so programs started afterwards don't inherit it. It stays readable in the reading process's own `/proc` environment, which comes with choosing `env:`. `keyring:` doesn't have that problem |
| R3 | **Backups stage a plain export on disk.** Each backup writes the per-conversation export to `<state>/backup/staging` before restic encrypts it. That export holds the rows as stored, unlocked conversations' keys included. It's removed afterwards (and at the next backup, after a crash) | Medium | **Fixed the same day (I40):** staged in memory (`$XDG_RUNTIME_DIR/cairnward-backup-staging`) and removed even when restic fails. It falls back to disk, with a warning, only when there's no memory folder, or by choice with `[backup] staging = "disk"` for exports larger than memory. Tested with real restic, including a failing run |
| R4 | **The passphrase guards everything at rest,** and the wrapped key travels in backups (beside the encrypted audit copy) | Medium, accepted | Offline guessing is slowed by age's scrypt, but an 8-character minimum is short. The docs say to use a long passphrase |
| R5 | **The key lives in memory** for the life of each process that opened the databases. It can reach swap or a core dump | Low, accepted | Same as the conversation keys of unlocked conversations today. Full-disk encryption, including swap, covers it |
| R6 | **No plaintext audit mirror while encrypted.** `audit verify` can't cross-check against `audit.jsonl` | Low, accepted (by design) | The hash chain and anchors with Merkle roots still detect changes. `audit verify` notes the missing mirror |

## Checked and found sound
- **Agents, MCP servers and plugins never receive the key:** their environments are built from
  allow-lists (`run/runner.py`, `mcp/launch.py`), and since R1 the core's environment has no key
  either.
- **A wrong key fails at connect,** before any query (`DatabaseKeyError`). A wrong passphrase
  can't silently create a new key.
- **An interrupted encryption can't lose the key:** the wrapped key is written (fsynced) as
  `.pending` before any file is converted, and `store encrypt` finishes the run.
- **`[storage] key` accepts references only;** a key written into the config file is refused at
  load.
- **The key reference is visible:** warned in `serve`, `doctor` and the dashboard, and recorded in
  settings snapshots, so turning it on is audited (D55).

## Not covered
- **Replication** with encrypted databases.
- **An outside review** of SQLCipher's own security. Its page HMAC and per-page IVs are relied
  on as documented by its authors.
- **Windows and macOS:** the driver exists for Linux x86-64 only.
