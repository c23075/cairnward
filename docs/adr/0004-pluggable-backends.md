# ADR-0004: Pluggable model backends via OpenAI-compatible and Ollama drivers

- Status: Accepted
- Date: 2026-10-31
- Spec: §4.1, D5, D19, D29

## Context
The user runs `qwen3:30b` on Ollama today on a CPU-only 32 GB machine, expects hardware and models to change, and may want to offload sub-agents to LAN or hosted servers. Almost every server (llama.cpp, vLLM, SGLang, LocalAI, llamafile, hosted providers) speaks the OpenAI-compatible API.

## Options
1. Ollama only: simplest; locks out llama.cpp's better CPU/MoE controls and hosted offload.
2. One driver per server: maximum features; lots of code.
3. Two drivers (`openai_compat`, `ollama`) + named backends in config + per-role/per-agent selection.

## Decision
Option 3. Backends have a `location` (`local`, `lan`, `hosted`); non-local backends are egress and go through policy, redaction and audit; hosted backends are denied by default and never receive locked chats. A dedicated `anthropic` driver may be added later (Claude API key; subscriptions can't be used).

## Consequences
- Swapping server or model is a config change.
- Server-specific features (Ollama model pulls, llama.cpp grammars, cache reuse) are optional capabilities detected per backend.
- Each provider's tool-calling quirks need integration tests.
