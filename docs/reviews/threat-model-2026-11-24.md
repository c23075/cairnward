# Threat model review, 2026-11-24 (NFR-S2)

Covers two controls added for accepted risks that the owner asked to close:
- **TLS inspection without isolation** (I37, D85): a new refusal on an existing flow, the run's
  start.
- **Environments per client** (I39, D86): a new check on the gateway's and MCP's request flows,
  and a change to `cairnward client add`.

Neither adds a component, data flow or trust boundary. Both narrow what an existing one allows.

**How it was reviewed:**
- Every place a run gets the CA, or a request picks its environment, was traced in the code:
  the runner, the core's management API, egress sessions, the chat and embeddings routes,
  `/v1/models` and the MCP routes.
- Tests drive the real config loader, the real gateway and management apps through ASGI, and
  the real audit log (`tests/test_tls_without_isolation.py`, `tests/test_client_environments.py`).
  Each fails with its fix switched off. Model backends are a fake, as in the other gateway
  tests. Nothing here ran with the real sandbox or a real client.
- The reviewer is the coding assistant that wrote the change, so an outside review is still
  outstanding (TASK-9).

## What changed in the model
- **T-33** (the CA key): its residual risk now applies only where the owner sets
  `allow_without_isolation`.
- **New threat T-40:** a client's own key names a more open environment.
- **Accepted risks:** both entries updated.
- Version 0.8.

## Findings

| # | Finding | Severity | State |
|---|---|---|---|
| R1 | **The refusal has to be in the core, not only in `cairnward run`.** Runs also start through the management API, and a plugin or script could ask for one directly | Medium | **Done.** The core refuses (`403`, `run.refused` in the audit log) before it issues a key or an egress session. `cairnward run` refuses first only to say why sooner |
| R2 | **Refusing, not skipping inspection.** Running without inspection would drop the environment's path rules silently, a weaker result than the owner configured | Medium | **Done.** The run is refused, so nothing runs with fewer checks than configured |
| R3 | **Keys made before this change stay unlimited.** Limiting them automatically could break clients that use another environment today | Low | **Accepted.** `doctor` warns about each one that can reach a more open environment, and TASK-16 asks the owner to limit them |
| R4 | **The default install warns.** `scratch` is more open than `coding`, so every unlimited key gets a `doctor` warning | Low | **Intended.** It points at the limit to set |
| R5 | **MCP refusals aren't audited,** like MCP's existing run-environment refusals; chat and embeddings refusals are | Low | **Open.** Recorded as TASK-28 |
| R6 | **An environment's own openness isn't checked:** a client limited to an open environment is still open | Low | **Accepted.** That is the owner's choice of environment, already shown by its privacy label |
