# ADR-0006: Encryption with age, key wrapping and crypto-shredding

- Status: Accepted
- Date: 2026-10-31
- Spec: §4.5, §5.5, D23, D24, D31, D35, D36

## Context
The user wants encryption **off by default**, passphrases **only for chats they lock**, chats and artifacts lockable separately, and only the user (directly or by explicitly asking the agent) able to turn encryption on. Separately, deleting data by overwriting is unreliable on SSDs, and the earlier draft used a custom XChaCha20 container format.

## Options
1. Custom format with libsodium primitives: flexible; our own format to get right.
2. **age** (a small, reviewed file-encryption format with X25519 and scrypt-passphrase recipients) via pyrage.
3. SQLCipher for the whole DB: all-or-nothing; conflicts with "off by default, per-chat opt-in".

Deletion options: overwrite + VACUUM (best effort) vs **crypto-shredding** (encrypt with a per-item key; delete the key).

## Decision
- age for all encrypted data.
- Every chat gets a random chat key (age identity); its records are always encrypted with it.
- Unlocked: the chat key is stored without a passphrase (no prompts; enables crypto-shredding).
- Locked: the chat key is wrapped with the passphrase (age scrypt recipient) and optionally a recovery key; passphrase changes re-wrap only.
- Artifacts have a separate key, locked separately.
- Purge = destroy the key.
- SQLCipher stays an optional Phase 3 "encrypt everything" mode.

## Consequences
- Reliable deletion for chat records regardless of lock state.
- Unlocked chats' plaintext search index/embeddings remain a best-effort deletion gap (documented).
- "Encryption off by default" in user-facing terms means "no passphrase"; the UI must not claim unlocked chats are protected against disk access.
