# ADR-0019: SQLCipher "encrypt everything" mode

- Status: Proposed (storage layer built 2026-11-10; not yet switchable or wired in)
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

## Decision (proposed)
Option 2, as an optional mode, off by default like every protection (D55):
- **Driver:** `sqlcipher3-binary` (MIT), an optional extra (`uv sync --extra sqlcipher`),
  hash-locked in `uv.lock`. Its only wheel is Linux x86-64, so the mode is Linux x86-64 only.
- **Key:** a random 256-bit key used raw (`PRAGMA key = "x'…'"`), so no passphrase derivation
  runs on each connection.
- **Conversion:** `sqlcipher_export` into a new file, then an atomic swap, in both directions.
  Existing data is converted without an export and re-import.
- **Crypto-shredding stays per conversation (ADR-0006).** SQLCipher adds a layer around it and
  doesn't replace it.

**Open question for the owner: where the database key lives.** Each option changes what an
unattended `serve` can do:
- **a. A secret reference** (`env:` or `keyring:`, like other secrets): unattended starts work.
  The key is only as safe as that store, and `keyring:` usually fails under WSL (I11).
- **b. Wrapped with a passphrase** (age scrypt, like a locked conversation) and typed at `serve`
  start: strongest at rest, but no unattended start, and scheduled backups wait for it.
- **c. Both:** the passphrase-wrapped key is the source of truth; a secret reference is an
  optional, audited weakening for unattended hosts. **Recommended**, as it matches how other
  protections are switchable and visible.

## Consequences
- **Built:** `store/db.py` has `make_engine(path, key)`, `connect(path, key)`,
  `is_encrypted(path)` and `convert(path, key, encrypt=…)`. A wrong or missing key fails at
  connect, with a clear error.
  - **Tested with the real driver** (`tests/test_sqlcipher.py`): content and schema absent from
    the file; WAL, `secure_delete` and Alembic migrations working on an encrypted file;
    conversion both ways keeping data and migration state; a failed conversion leaving the file
    unchanged.
- **Still to do before the mode can be turned on:**
  - a `[storage] encryption` setting with privacy-preset values, shown in `privacy show` and
    audited;
  - the key source (the open question above);
  - passing the key to the vault, keys and audit stores, including the sandboxed core in
    isolated mode;
  - `backup.py`, which reads the databases with `sqlite3` directly (snapshots should hold the
    per-conversation export, as now, not the encrypted files);
  - replication;
  - `doctor` checks;
  - `cairnward store encrypt|decrypt`.
- **Costs:** SQLCipher is slower than plain SQLite (to be measured against the Strict overhead
  target, I14). Tools that read the files directly (`sqlite3`, DB browsers) stop working.
- **Converting doesn't erase the old plaintext** from the disk's free space. Full-disk encryption
  is still advised.
