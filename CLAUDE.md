# Cairnward: notes for Claude sessions

A local privacy gateway (model gateway, egress proxy, policy engine, audit log, transcript
vault, MCP proxy, plugin contract) for one person's machine. Literature research is a separate project
(`literature-review-agent-plugin`, ADR-0017): nothing here may depend on it.
Python 3.12+, uv, Apache-2.0. Docs index by reader: `docs/README.md`; overview and stack
position: `docs/overview.md`. Design: `docs/spec.md` (decisions D1–D80), ADRs in
`docs/adr/`, requirements in `docs/requirements.md`, plans in `docs/plan/`. The current
state and next steps are in the newest `docs/plan/handoff-*.md`; requirement status is in
`docs/plan/requirements-status.md`, so a new handoff doesn't copy it. `docs/status.md` is the
owner's view of the project: keep it current when issues or test results change.

## Commands
```bash
uv sync
uv run ruff check . && uv run ruff format --check . && uv run mypy   # CI's lint job
uv run pytest -q                                                    # ~4 min
uv run cairnward config schema   # after changing anything in config/models.py (schemas/ is committed)
cd rust && cargo fmt --all --check && cargo clippy --all-targets -- -D warnings && cargo test && cargo build
uv run pytest -q tests/test_egress_rust.py tests/test_egress.py   # both data paths (needs the cargo build)
```
- Isolated-mode tests (`tests/isolation/`) skip unless the sandbox is found: run
  `cairnward sandbox install`, or point at the copy in the repo:
  `export CAIRNWARD_SRT="node $PWD/spikes/t06-sandbox/node_modules/@anthropic-ai/sandbox-runtime/dist/cli.js"`
  (needs bubblewrap, socat, ripgrep).
- Browser test (`tests/e2e/`) skips unless node can load playwright and axe-core:
  `cd tests/e2e && npm ci && npx playwright install chromium`.
- A long state-dir path breaks Unix sockets (107 bytes); tests use short tmp paths.

## Conventions
- mypy strict, ruff (line length 100). Hand-aligned calls keep `# fmt: skip`.
- Every privacy/security feature is switchable (D55); weakening is visible (privacy
  warnings, dashboard highlight) and audited.
- Secrets never go in config files (use `${env:NAME}` or `env:`/`keyring:` refs), the audit
  log, or reports. Audit records hold digests of prompts/queries, not text.
- Third-party downloads are hash-pinned (uv.lock, `redaction_engines/locks/`,
  `tests/e2e/package-lock.json`, `rust/Cargo.lock`, checked by `cargo audit` in CI).
- Docs are plain and specific: say what was tested for real and what only against a
  stand-in. Update `docs/spec.md` (and the ADR, if a decision changes) with the code.
- Commit only when asked; never put model identifiers in commits or docs.

## Testing lessons
- Mock transports open no sockets and hid two real bugs (HTTP clients closed in a second
  event loop; Presidio's worker loading a model that wasn't installed). For anything that
  talks HTTP or starts a worker, add at least one test against a real local server or the
  real package.
- Fakes must use the real data formats (a page label is "p. 2" / "pp. 2-3", not "2").
