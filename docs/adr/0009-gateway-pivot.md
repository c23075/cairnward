# ADR-0009: Pivot to a client-neutral privacy gateway plus research workflows

- Status: Accepted
- Date: 2026-10-31
- Spec: §1, §3, §4, D42–D44
- Supersedes: ADR-0002; the thin-client part of ADR-0005

## Context
Spec v0.5 described an all-in-one local agent: own TUI and web chat clients, own coding flow, research tools, sync, encryption, multi-agent, about 90 requirements for a likely single maintainer. A review of existing projects showed that chat apps (Open WebUI, Jan, LibreChat, AnythingLLM), coding agents (Qwen Code, opencode, Aider, Goose, OpenHands) and research assistants (PaperQA2, Zori, ZotLLM) already cover most features. None of them provides an enforced egress policy, a verifiable per-action audit log, and an egress report across tools.

## Options
1. All-in-one as specified (high risk of many mediocre features).
2. All-in-one with a narrow MVP (one client, one use case).
3. Gateway only: privacy and audit layer for other tools.
4. Gateway + own research workflows.

## Decision
Option 4. Cairnward provides an OpenAI-compatible model endpoint (with virtual models for environment × effort), an MCP proxy, an OS-enforced egress proxy, policy and approvals, audit log, transcript vault, artifacts, sync/backup and a dashboard. Chat and coding UIs come from existing tools. Cairnward's own product work focuses on literature-review workflows, where existing tools are weakest and CPU-only hardware fits batch processing.

## Consequences
- Much smaller UI surface (one dashboard); the security core becomes the product and can be tested thoroughly.
- Claude-app-style features (folders, shortcuts, pickers) depend on the chosen client, except in the vault browser and via virtual models.
- New work: client/agent profiles, compatibility tests, conversation reconstruction for the vault.
- Third-party clients and agents become untrusted components (threat model boundary B7).
