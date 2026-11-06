# Contributing

Thanks for helping. Cairnward is a small project, run by one person, so please read this before
opening a pull request. Security problems don't go here: see [SECURITY.md](SECURITY.md).

## Set up and check your change

You need Python 3.12 or later, and [uv](https://docs.astral.sh/uv/).

```bash
uv sync
uv run ruff check . && uv run ruff format --check . && uv run mypy   # what CI's lint job runs
uv run pytest -q                                                    # about 4 minutes
```

**If you change anything in `src/cairnward/config/models.py`,** regenerate the committed schemas:
`uv run cairnward config schema`.

**Two kinds of test skip** unless you set them up:
- **Isolated-mode tests** (`tests/isolation/`) need the sandbox, and bubblewrap, socat and
  ripgrep. Run `uv run cairnward sandbox install`, or point at the copy in the repository:
  ```bash
  (cd spikes/t06-sandbox && npm ci)
  export CAIRNWARD_SRT="node $PWD/spikes/t06-sandbox/node_modules/@anthropic-ai/sandbox-runtime/dist/cli.js"
  ```
- **The browser test** (`tests/e2e/`) needs Playwright and axe-core:
  `cd tests/e2e && npm ci && npx playwright install chromium`.

CI runs all of them, on Linux, Debian and Windows.

## Proposing a change

- **Open an issue first** for anything bigger than a fix, so we can agree on the approach.
- **Work on a branch,** one change per pull request. Small PRs get reviewed sooner.
- **Fill in the PR template:**
  - say what you tested for real and what only against a stand-in;
  - tick or explain each checklist item: the spec, the threat model, switchable protections, no
    secrets.
- **Link the issue** with `Fixes #<number>` so it closes on merge.
- **CI must pass** before a PR is merged.

## Conventions

**Code**
- mypy strict, ruff, line length 100. Hand-aligned calls keep `# fmt: skip`.
- **Every privacy or security feature is switchable.** Weakening one must be visible (a privacy
  warning, the dashboard's highlight) and audited.
- **Secrets never go** in config files (use `${env:NAME}`, `env:` or `keyring:` references), the
  audit log, or reports. Audit records hold digests, not text.
- **Third-party downloads are pinned by hash:** `uv.lock`, `redaction_engines/locks/`, and the npm
  lockfiles.

**Tests**
- A bug fix comes with a test that fails without the fix.
- For anything that talks HTTP or starts a worker, test against a real local server or the real
  package, not only a mock.
- Fakes use the real data formats.

**Docs change with the code**
- Update `docs/spec.md`, and the ADR in `docs/adr/` if a design decision changes.
- If a change touches a component, data flow or trust boundary, update
  `docs/threat-model.md` and record the review in `docs/reviews/`.
- Write plainly and specifically.

**Commits**
- A subject in the imperative, under about 70 characters.
- A body that says why.
- If an AI assistant helped, credit it with a `Co-Authored-By` trailer. Don't put model names or
  version identifiers in commits or docs.

## Issues and the status page

There are two places where work is tracked:
- **GitHub issues** take reports from anyone.
- **The I-numbers in [docs/status.md](docs/status.md)** are the owner's list of known issues.

**The rule linking them:** a GitHub issue that changes the project's state also gets an
I-number, and each refers to the other. Bug #36 and I35 are an example. A question or a declined
request stays a GitHub issue only.

## Licence

Cairnward is licensed under Apache-2.0. By contributing, you agree your contribution is licensed
the same way.
