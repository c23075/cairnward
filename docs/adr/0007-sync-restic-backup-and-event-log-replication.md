# ADR-0007: Sync: restic for backup, per-device event logs for replication

- Status: Accepted; amended 2026-10-31 by spec D50: purging also rewrites restic snapshots (`restic rewrite` + `prune`)
- Date: 2026-10-31
- Spec: §4.12, D27, D30, D40

## Context
The user wants chats and artifacts synced, with modes none / on demand / automatic-scheduled. The v0.4 draft described custom bundle merging with conflict rules. Home-grown sync and merge logic is a common source of data loss.

Two different needs hide inside "sync": **backup/restore** and **the same chats on several machines**.

## Options
1. Custom bundles + merge rules (v0.4).
2. CRDT library (Automerge) for everything: strong convergence guarantees; more machinery than append-only chats need.
3. **restic** for backup; **per-device append-only event logs** (event sourcing) for chat replication; **git** for artifacts.

## Decision
Option 3.
- `backup` jobs: restic snapshots (always encrypted, deduplicated, retention, integrity checks); cloud targets through restic's rclone backend.
- `replicate` jobs: each device writes only its own event-log files; state is rebuilt by a deterministic merge (Lamport clock, device ID); concurrent turns fork the chat; artifacts replicate as git history.
- Transport for both: local folder/NAS/Syncthing, rclone (Drive), or git remote.
- Automerge is the fallback if collaborative document editing is ever needed.

## Consequences
- No two devices ever write the same file, so file-level conflicts disappear.
- Merge convergence is testable with property-based tests.
- restic's mandatory encryption means backups always need a password (stored in the keychain); this is the user's opt-in when creating a backup job.
- restic and rclone become optional external dependencies.
