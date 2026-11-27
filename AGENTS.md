# Cairnward: notes for coding agents

These are the instructions for any AI coding agent working in this repository. CLAUDE.md points
here, so edit this file, not that one.

A local privacy gateway (model gateway, egress proxy, policy engine, audit log, transcript
vault, MCP proxy, plugin contract) for one person's machine. Literature research is a separate project
(`literature-review-agent-plugin`, ADR-0017): nothing here may depend on it.
Python 3.12+, uv, Apache-2.0. Docs index by reader: `docs/README.md`; overview and stack
position: `docs/overview.md`. Design: `docs/spec.md` (decisions D1–D86), ADRs in
`docs/adr/`, requirements in `docs/requirements.md`, plans in `docs/plan/`. All pending work is
in `backlog/`, kept with Backlog.md (ADR-0022); what each session did is in `docs/plan/progress.md`;
requirement status is in `docs/plan/requirements-status.md`. `docs/status.md` is the owner's
view: its action, agent and testing sections are generated from the task list, and the rest
(known issues, recent changes) is kept current by hand.

## Each session
**Start:**
1. `git log --oneline -15` and the top two entries of `docs/plan/progress.md`.
2. `uv run python scripts/tasks.py next` for open work, most urgent first. When the owner asks
   what's pending, answer from this, not from older handoff files.

**End:**
1. Update the task list with the Backlog.md CLI (below) or its MCP tools, never by editing
   `backlog/` files by hand: mark what you finished `Done`, and add new work. Change a task's
   wording only if the owner asks.
2. `uv run python scripts/tasks.py render`. CI fails if status.md is behind the list, or a task
   breaks the rules in `scripts/tasks.py`.
3. Add an entry at the top of `docs/plan/progress.md`: what changed, what ran for real and what
   only against a stand-in, what was found, and the task IDs it touched.
4. Update status.md's known issues and recent changes if they changed.

## The task list (Backlog.md)
Install the pinned CLI once per clone: `cd tools/backlog && npm ci --ignore-scripts`. Then, with
`B=tools/backlog/node_modules/.bin/backlog`:
```bash
$B task list --plain -s "To Do" --sort priority        # open tasks
$B task view TASK-12 --plain
$B task edit TASK-12 -s Done                             # finished
$B task create "What to do" --type work -a @agent --priority medium -l egress -d "Why it matters"
$B task create "What the owner does" --type action -a @owner --priority high -l backup -d "Why"
$B task create "What to run for real" --type check -a "" -l never-run-for-real,sandbox
$B task edit TASK-12 --add-label security -m "Production ready"   # another topic; a milestone
$B milestone list --plain
$B task edit TASK-29 --dep TASK-10                       # waits on another task
$B browser                                               # the board, on 127.0.0.1
```
- **Types:** `action` is the owner's (`-a @owner`), `work` is an agent's (`-a @agent`), `check` is
  something to run for real, with one group label (see `backlog/config.yml`). Open actions and
  work need a priority (High, Medium, Low) unless they wait on another task.
- **Topics:** every task has at least one topic label, such as `backup`, `release`, `repo`,
  `security`, `egress` or `wsl2` (the full list is in `backlog/config.yml`; add one there if none
  fits). `scripts/tasks.py next --topic backup` shows one topic.
- **Milestones:** `0.1.0 release` and `Production ready`. Put a task in one when it has to be done
  before that milestone; status.md shows each milestone's progress.
- **Agents with MCP** can use `$B mcp start` (stdio) instead of the CLI.
- **Keep titles short** (the file is named after it) and put details in the description. Links
  in a task are from the repository root: `[ops](/docs/operations.md)`.
- IDs before 2026-11-25 were A, W and C numbers; each task's notes keep its old one, and the
  progress log has the map.

## Commands
```bash
uv sync
uv run pre-commit install        # once per clone: secret scan and ruff before each commit
uv run ruff check . && uv run ruff format --check . && uv run mypy   # CI's lint job
uv run pytest -q                                                    # ~4 min
uv run cairnward config schema   # after changing anything in config/models.py (schemas/ is committed)
cd rust && cargo fmt --all --check && cargo clippy --all-targets -- -D warnings && cargo test && cargo build
uv run pytest -q tests/test_egress_rust.py tests/test_egress.py tests/test_audit_rust.py   # Rust and Python agree (needs the cargo build)
uv run python scripts/bench_egress.py --dir <folder on the data disk>   # both egress data paths
```
- Isolated-mode tests (`tests/isolation/`) skip unless the sandbox is found: run
  `cairnward sandbox install`, or point at the copy in the repo:
  `export CAIRNWARD_SRT="node $PWD/spikes/t06-sandbox/node_modules/@anthropic-ai/sandbox-runtime/dist/cli.js"`
  (needs bubblewrap, socat, ripgrep).
- Browser test (`tests/e2e/`) skips unless node can load playwright and axe-core:
  `cd tests/e2e && npm ci && npx playwright install chromium`.
- A long state-dir path breaks Unix sockets (107 bytes); tests use short tmp paths.
- **In a fresh container** many tests skip quietly. Install first: `apt-get install restic
  openssh-client bubblewrap socat ripgrep`; `npm ci` in `spikes/t06-sandbox` with `CAIRNWARD_SRT`
  set as above; `cargo build --manifest-path rust/Cargo.toml` for the Rust differential tests.
  SQLCipher comes with `uv sync` on Linux x86-64.
- `mypy --platform win32` reports errors for Linux-only calls (`flock`, `killpg`, Unix sockets) in
  `backup.py`, `replication.py`, `mcp/launch.py`, `run/runner.py` and `core/server.py`. CI's mypy
  runs on Linux only.

## Conventions
- mypy strict, ruff (line length 100). Hand-aligned calls keep `# fmt: skip`.
- Every privacy/security feature is switchable (D55); weakening is visible (privacy
  warnings, dashboard highlight) and audited.
- Secrets never go in config files (use `${env:NAME}` or `env:`/`keyring:` refs), the audit
  log, or reports. Audit records hold digests of prompts/queries, not text.
- Third-party downloads are hash-pinned (uv.lock, `redaction_engines/locks/`,
  `tests/e2e/package-lock.json`, `rust/Cargo.lock`, checked by `cargo audit` in CI). So are
  GitHub Actions and pre-commit hooks: a commit hash with the version in a comment.
- Docs are plain and specific: say what was tested for real and what only against a
  stand-in. Update `docs/spec.md` (and the ADR, if a decision changes) with the code.
- Commit only when asked; never put model identifiers in commits or docs.

## Working in the repository
- Work on a branch and open a pull request; never commit to `main`. The owner merges.
- Never skip the pre-commit hooks (`--no-verify`). If gitleaks flags a value, treat it as
  real: tell the owner, and don't commit it. A made-up value a test needs goes in
  `.gitleaks.toml`, matched exactly.
- The repository's practices and their status are in `docs/repository-management.md`.

## Testing lessons
- Mock transports open no sockets and hid two real bugs (HTTP clients closed in a second
  event loop; Presidio's worker loading a model that wasn't installed). For anything that
  talks HTTP or starts a worker, add at least one test against a real local server or the
  real package.
- Fakes must use the real data formats (a page label is "p. 2" / "pp. 2-3", not "2").
