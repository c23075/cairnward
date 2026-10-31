# ADR-0011: Transcript vault for conversations from any client

- Status: Accepted
- Date: 2026-10-31
- Spec: §4.5, D47

## Context
With a client-neutral gateway, conversations live in each client's database, which Cairnward can't lock, sync or reliably delete. The user wants chat encryption (opt-in), sync and reliable deletion regardless of client.

## Options
1. Transcript vault: Cairnward stores every conversation passing through its model endpoint.
2. Cairnward data only: audit, research outputs, artifacts; chat history is the client's job.
3. Metadata only: no conversation content stored by Cairnward.

## Decision
Option 1. The OpenAI API is stateless (each request carries the full history), so conversations are reconstructed by hashing message prefixes: a request extending a known prefix extends that conversation; a diverging prefix creates a branch. Records use per-conversation age keys (crypto-shredding), can be locked with a passphrase, synced and purged (including backup rewrite). Per-environment setting `vault = "off"` stores only audit digests (option 3) for sensitive work.

## Consequences
- One searchable, lockable history across all clients and agents.
- Conversations exist twice (vault + client); setup guides minimise client-side history; the UI states that deleting in the vault doesn't delete client copies.
- Prefix reconstruction must handle clients that rewrite history (e.g. system-prompt changes, compaction); those appear as branches.
