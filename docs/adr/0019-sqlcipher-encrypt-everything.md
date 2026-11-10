# ADR-0019: SQLCipher "encrypt everything" mode

- Status: Accepted 2026-11-10 (key source: option c); implemented as spec D80
- Date: 2026-11-10
- Spec: §4.5, §5.5, §9; amends [ADR-0006](0006-encryption-age-key-wrapping-crypto-shredding.md)

## Context
ADR-0006 encrypts message bodies with a per-conversation age key, and stores unlocked
conversations' keys unwrapped in `keys.db`. On disk that leaves readable:
- **In `vault.db`:** titles, folders, timestamps, ids, sizes and the shape of each conversation.
- **In `keys.db`:** every unlocked conversation's key.
- **In `audit.db`:** every record's metadata.

Someone with the disk (a lost laptop without full-disk encryption, a copied data folder, a backup
of the folder outside restic) gets all of that, and with the keys, the unlocked conversations.
ADR-0006 deferred a whole-database mode as "Phase 3".

## Options
1. **Rely on full-disk encryption:** no work, but nothing protects a copied folder, and Cairnward
   can't check it's on.
2. **SQLCipher for `vault.db`, `keys.db` and `audit.db`:** every page encrypted (AES-256, HMAC
   per page). The keys and metadata above are no longer readable from the files.
3. **Encrypt more fields with age inside plain SQLite:** finer-grained, but the schema, sizes and
   indexes still leak, and every query on an encrypted field gets harder.

## Decision
Option 2, as an optional mode, off by default like every protection (D55):
- **Driver:** `sqlcipher3-binary` (MIT), an optional extra (`uv sync --extra sqlcipher`),
  hash-locked in `uv.lock`. Its only wheel is Linux x86-64, so the mode is Linux x86-64 only.
- **Key:** a random 256-bit key used raw (`PRAGMA key = "x'…'"`), so no passphrase derivation
  runs on each connection.
- **Conversion:** `sqlcipher_export` into a new file, then an atomic swap, in both directions.
  Existing data is converted without an export and re-import.
- **Crypto-shredding stays per conversation (ADR-0006).** SQLCipher adds a layer around it and
  doesn't replace it.

**Where the database key lives: option c, chosen by the owner (A16).** The options were:
- **a. A secret reference** (`env:` or `keyring:`, like other secrets): unattended starts work.
  The key is only as safe as that store, and `keyring:` usually fails under WSL (I11).
- **b. Wrapped with a passphrase** (age scrypt, like a locked conversation) and typed at `serve`
  start: strongest at rest, but no unattended start, and scheduled backups wait for it.
- **c. Both (chosen):** the passphrase-wrapped key is the source of truth; a secret reference is
  an optional, visible weakening for unattended hosts, as other protections are switchable and
  visible.

## Consequences
Implemented as spec D80 (§5.5), where the commands, the key handling and what was tested are
described.
- **The audit mirror goes:** an encrypted audit log keeps no plaintext `audit.jsonl` mirror, since
  it would hold what the encryption hides. `audit verify` loses its cross-check against the
  mirror and notes that.
- **Isolated mode:** the outer process writes the key to the sandboxed core's standard input,
  read once at start. It was first an environment variable, but the 2026-11-10 review (R1)
  found it readable in `/proc/<pid>/environ`. Agents, MCP servers and plugins never receive it.
- **Costs** (measured): vault writes about 20% slower, reads about 7%. Audit appends got faster
  without the mirror. Tools that read the files directly stop working.
- **Linux x86-64 only,** because of the driver's wheels.
- **Converting doesn't erase the old plaintext** from the disk's free space or older backups.
  Full-disk encryption is still advised.
- **Tested in isolated mode with the real sandbox.** Not yet tried with replication.
