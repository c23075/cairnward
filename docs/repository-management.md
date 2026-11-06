# Managing the repository

Written 2026-11-06; checklist status checked the same day. Each section gives a widely used practice for running a code repository, then
how this repository handles it today and what's missing. The owner's view of the project is
[status.md](status.md); this page is about the repository itself. Settings marked "check" can't be
seen from the code, so confirm them under the repository's Settings on GitHub.

## Summary

**Done well:**
- CI pins every action by commit hash, runs with read-only permissions, refuses an out-of-date
  lockfile, and tests Linux, Debian and Windows. It also runs the isolated sandbox and the browser.
- Dependencies are hash-locked, updated weekly by Dependabot, and audited for vulnerabilities and
  licences.
- Docs, ADRs and the threat model are updated with the code, and the PR template checks for it.

**To fix, in order:**
1. Protect `main`, so nothing merges before CI passes. PRs 22 to 24 did, and broke `main` (I25).
2. Turn on automatic deletion of merged branches, and delete the 31 merged ones.
3. Make the first release (A8). There are no tags yet, and the version is still 0.0.1.
4. Decide how GitHub issues and the status page's I-numbers relate.
5. Add a short CONTRIBUTING.md, and issue templates.

## 1. Protect the default branch

**The practice:** nobody pushes to `main` directly. Every change arrives by pull request, merges
only when the required checks pass on its latest commit, and `main` can't be force-pushed or
deleted.

**Here:**
- Every change in this project's recent history came through a PR.
- **Check: required checks may not be on.** PRs 22 to 24 were merged before CI had run, and broke
  `main` until PR 25 fixed it (I25).

**To do:** under Settings → Rules → Rulesets (or Branches), for `main`:
- require a pull request before merging;
- require these status checks to pass, and require the branch to be up to date:
  - `lint`, `audit`, `test-debian`, `dashboard-e2e`, `isolation-spike`;
  - the four `test` jobs (Ubuntu and Windows, Python 3.12 and 3.13);
- block force pushes and deletion;
- require approvals only once there's a second maintainer. A sole owner can't approve their own
  PR, so for now, required checks are the gate.

## 2. Branches

**The practice:**
- Short-lived topic branches, one per change, named for the change.
- Deleted when merged.
- No long-lived side branches.

**Here:**
- Branches are short-lived and named for the change (`replication`, `wsl2`, `fix-main-ci`).
  Branches made by AI coding sessions are named `claude/...`.
- **Merged branches remain on GitHub.** There were 31 on 2026-11-06, all contained in `main`.
  By the next check that day, 18 had been deleted and 13 remained.
  `wip/mcp-oauth` holds an early commit whose change is already in `main` (as `d1124dc`).

**To do:**
- Turn on Settings → General → "Automatically delete head branches".
- Delete the merged branches. Either use Code → Branches → Stale, or run:
  ```bash
  git fetch --prune origin
  git branch -r --merged origin/main | grep -v -e 'origin/main' -e HEAD \
    | sed 's#origin/##' | xargs -n1 git push origin --delete
  ```
- On each machine, run `git fetch --prune`, `git branch --merged main` with `git branch -d`, and
  `git worktree prune`. The desktop had 17 old worktrees before this was last done.

## 3. Pull requests

**The practice:**
- A PR does one thing, small enough to review in one sitting.
- It says what changed and why, and how it was tested.
- It links its issue, with `Fixes #n` so the issue closes on merge.
- It merges only when green.

**Here:**
- `.github/pull_request_template.md` asks for:
  - what and why;
  - how it was tested, separating real tests from stand-ins;
  - a checklist: spec updated, threat model reviewed (NFR-S2), protections switchable, no secrets.
- PRs link issues: #38 fixed #36, which closed on merge.
- **One gap: review.** Every PR so far was written by the owner or an AI coding assistant and
  reviewed by the same people, so no independent person reviews the code (A10). The template's
  threat-model box was once left unchecked without the work being done (I32), and that was found
  only later.

**To do:**
- Keep PRs to one subject. PR 28 (TLS inspection and OpenShell) was large enough that its
  threat-model review was missed.
- When the template's threat-model box applies, do the review in the same PR.

## 4. Commits

**The practice:**
- A subject line in the imperative, under about 70 characters.
- A body that says why, not only what.
- One logical change per commit.
- Credit co-authors with trailers.

**Here:**
- Subjects follow this ("Fix I15: start the isolated core faster").
- Bodies explain the cause and how it was tested.
- Commits made with an AI assistant carry a `Co-Authored-By` trailer and a session link.
- CLAUDE.md adds two rules: never put model identifiers in commits or docs, and commit only when
  asked.
- The commit history is public and shows the owner's personal email addresses. The owner decided
  to keep them (A13, 2026-11-05).

**To do:** nothing required. GitHub's no-reply address (Settings → Emails) would keep new commits
from showing a personal address.

## 5. How PRs are merged

**The practice:** choose one merge method and keep to it:
- **merge commits**, which keep every commit and record the PR as a merge;
- **squash**, one commit per PR;
- **rebase**, a linear history.

**Here:** merge commits, 37 of them so far, which suits PRs whose individual commits are worth
keeping.

**To do:**
- Keep merge commits, and turn off the other two methods in Settings → General, so the history
  stays consistent.
- One consequence was seen on 2026-11-05: a PR merged at an earlier commit than the branch's
  latest, leaving a commit off `main`. Merge only once the PR shows its final commit.

## 6. Continuous integration

**The practice:**
- CI runs on every PR and on every push to `main`.
- It covers each supported platform, and can't be weakened by the code it tests.
- Third-party actions are pinned to a commit hash.
- Workflows get the least permission they need.

**Here (`.github/workflows/ci.yml`):**
- **Hardened:**
  - every action pinned by commit hash, with its version in a comment;
  - `permissions: contents: read`, and `persist-credentials: false` on checkout;
  - `UV_FROZEN=1`, so an out-of-date lockfile fails;
  - concurrency cancels outdated runs.
- **Jobs:**
  - lint: ruff, ruff format and mypy;
  - tests on Ubuntu and Windows, Python 3.12 and 3.13, with coverage of at least 80% per package
    on Linux;
  - a Debian container without git;
  - a dependency audit (pip-audit and a licence check);
  - the dashboard in a real browser, with accessibility checks;
  - the isolation suite in the real sandbox.
- **Gaps:**
  - WSL2 isn't tested in CI (I19).
  - `core/server.py` is outside the coverage count (I18).
  - Nothing else: CodeQL code scanning (Python, JavaScript, workflows) runs on every PR, as seen
    on PR 39 (checked 2026-11-06).

**To do:**
- Make the jobs required checks (section 1).
- When adding a job, pin its actions the same way. Dependabot updates the pins (section 7).

## 7. Dependencies

**The practice:**
- Lock every dependency to an exact version and hash.
- Update on a schedule, in small batches.
- Audit for known vulnerabilities and licences in CI.

**Here:**
- **Locked by hash:**
  - Python packages in `uv.lock`;
  - optional redaction engines in `redaction_engines/locks/`;
  - Node packages for the browser test in `tests/e2e/package-lock.json`;
  - the sandbox in `src/cairnward/sandbox/srt_pkg/package-lock.json`.
- **Updated:** Dependabot opens weekly PRs for Python packages (development tools grouped) and for
  GitHub Actions.
- **Audited:** CI runs pip-audit, a licence allow-list check, and an audit of the sandbox's Node
  dependencies.

**To do:**
- Review Dependabot PRs like any other: CI must pass, and read the changelog of anything in the
  security path. That covers cryptography, httpx and h11, the MCP SDK, FastAPI and uvicorn, and the
  sandbox.
- Dependabot doesn't watch the npm lockfiles. Either add `npm` entries for `tests/e2e` and the
  sandbox package, or update them by hand when the sandbox version changes.

## 8. Security of the repository

**The practice:**
- A security policy with a private way to report problems.
- Secret scanning with push protection.
- Code scanning.
- Two-factor authentication for everyone with write access.
- No secrets in the repository, ever.

**Here:**
- `SECURITY.md` asks for private advisories (Security → Report a vulnerability). It also says
  plainly that there's no outside review, and that only the latest release gets fixes.
- **No secrets in the repository,** by design. Config uses `env:` and `keyring:` references, and
  CI checks that secrets never reach the audit log.
- **Code scanning is on:** CodeQL runs on every PR (seen on PR 39).
- **Check:** secret scanning and push protection (Settings → Code security), and two-factor
  authentication on the owner's account.
- **Deploy keys:** the audit-anchor deploy key belongs to a separate private repository, and can
  write only there. Keep it that way.

**To do:** turn on secret scanning with push protection, and Dependabot security alerts, if they
aren't on.

## 9. Releases and versions

**The practice:**
- Semantic versions.
- A changelog entry for every user-visible change.
- Releases built by CI from a tag, signed, with a software bill of materials.
- A person reads the draft before publishing.

**Here:**
- `CHANGELOG.md` has an "Unreleased" section, kept up to date.
- `.github/workflows/release.yml` builds on a `v*` tag, checks, signs, and leaves a draft
  release. Nothing is published to a package index.
- **There are no tags yet,** the version is still 0.0.1, and the release workflow has never run
  (A8).

**To do:**
1. Choose the first version, for example 0.1.0, and set it in `pyproject.toml`.
2. Move "Unreleased" in the changelog under that version.
3. Tag `v0.1.0` and push the tag.
4. Read the draft release, check the signatures, then publish.
5. From then on, tag each release the same way, so "only the latest release gets fixes" has
   something to point to.

## 10. Issues and tracking

**The practice:**
- One place where work is tracked, with labels, templates and a triage habit.
- Outside reports answered promptly.

**Here, two trackers:**
- [status.md](status.md) holds the owner's list (I-numbers for issues, A-numbers for the owner's
  actions). Every PR updates it.
- GitHub issues receive outside reports, such as #36 from an outside contributor, which became
  I35.

**To do:**
- Keep both, with a rule: an outside report stays a GitHub issue, and gets an I-number only when
  it changes the project's state. Link the two both ways, as with I35 and #36.
- Add issue templates for bug reports, feature requests and a pointer to `SECURITY.md`.
- Thank outside reporters on the issue when it's fixed.

## 11. Documentation kept with the code

**The practice:**
- Docs live in the repository and change in the same PR as the code they describe.
- Design decisions are recorded where they're made.

**Here:**
- CLAUDE.md requires updating `docs/spec.md`, and the ADR if a decision changes, with the code,
  and keeping `docs/status.md` current.
- Decisions are in `docs/adr/` and the spec's decision log (D1 to D78).
- Each working session ends with a handoff in `docs/plan/`.
- **Docs still drift:** on 2026-11-06, the spec's architecture diagram was out of date, and the
  threat model held two claims the code didn't support. Both were fixed, and
  [limitations.md](limitations.md) now collects the limits in one place.

**To do:** at each milestone, read the threat model and limitations page against the code, and
record that it was done in `docs/reviews/`.

## 12. Contributors and ownership

**The practice:**
- A CONTRIBUTING file that says how to set up, test and propose a change.
- CODEOWNERS when there's more than one maintainer.
- A clear licence.

**Here:**
- Apache-2.0 (`LICENSE`).
- CLAUDE.md serves as the setup and testing guide for AI coding sessions.
- There's no CONTRIBUTING.md and no CODEOWNERS.

**To do:**
- Add a short CONTRIBUTING.md for people, pointing to the commands in CLAUDE.md:
  - `uv sync`;
  - the lint commands, and the test suite;
  - regenerating schemas, and how to enable the isolation and browser tests;
  - the PR template's checklist.
- CODEOWNERS can wait until there's a second maintainer.

## 13. Working with AI coding agents

This repository is written mostly with an AI coding assistant, which adds some practices of its
own.

**The practice:**
- The agent works on a branch, never on `main`.
- A person decides what merges.
- The agent's instructions are in the repository, and agent-written commits are marked as such.

**Here:**
- **Instructions in the repository:** CLAUDE.md holds the agent's instructions (commands,
  conventions, testing lessons).
- **Branches and PRs:** each session works on a `claude/...` branch and opens PRs. The owner
  merges.
- **Marked commits:** agent commits carry `Co-Authored-By` and a session link.

**Watch for:**
- **Self-review:** the same assistant writes the code, the tests and the threat model (A10).
  Ask for findings to be reproduced. This session's bug fixes each came with a test that fails
  without the fix.
- **Agent sessions leave things behind:** branches, local worktrees, temporary folders. Clean
  them up (section 2).
- **Agent-written docs can claim more than the code does.** Check the claims against the code
  (section 11).

## 14. The related repository

Literature research is a separate repository, `literature-review-agent-plugin` (ADR-0017).

**When a change spans both:**
- **Merge order:** merge in the order the change needs. On 2026-11-05 the plugin's
  `disentangle-core` branch had to merge before the core's `disentangle-plugin`.
- **Coordination:** say the merge order in both PRs, and keep the plugin contract's version
  (`CAIRNWARD_CONTRACT`) in step.
- **Same practices:** apply this page to that repository too: branch protection, branch deletion,
  releases.

## Checklist

Status as checked on 2026-11-06, from the repository's files and GitHub's branch list. Settings
that GitHub doesn't show to this check are marked "can't see"; tick them yourself.

| # | Action | Where | Status |
|---|---|---|---|
| 1 | Protect `main`: require a PR, require the CI checks, block force pushes | Settings → Rules | ☐ GitHub lists `main` as not protected. A newer ruleset may not show in that flag, so look in Settings → Rules |
| 2 | Automatically delete head branches | Settings → General | ☐ Not on: PR 39's branch was still there after it merged |
| 3 | Delete the merged branches | Code → Branches, or the commands above | ◐ 18 of 31 deleted. 13 remain: `a11y-dialogs`, `citation-eval`, `client-budgets`, `dashboard-backup`, `gateway-overhead-bench`, `lit-ask-eval-fixes`, `mcp-oauth`, `measure-gaps`, `paperqa-timeout`, `release-workflow`, `review-screen-main-model`, `wip/mcp-oauth` (superseded), and `claude/project-status-kzw0ob` |
| 4 | Allow only merge commits | Settings → General | Can't see. Every merged PR so far (38) used a merge commit |
| 5 | Secret scanning with push protection, Dependabot alerts | Settings → Code security | Can't see. Code scanning (CodeQL) is on |
| 6 | First release: version, changelog, tag, publish the draft | `pyproject.toml`, `CHANGELOG.md`, a `v*` tag | ☐ No tags; the version is still 0.0.1 (A8) |
| 7 | Dependabot for the npm lockfiles | `.github/dependabot.yml` | ☐ Only `uv` and `github-actions` are watched |
| 8 | CONTRIBUTING.md and issue templates | Repository root, `.github/ISSUE_TEMPLATE/` | ☐ Neither exists |
| 9 | A rule linking GitHub issues and I-numbers | This page, section 10 | ☐ Proposed in section 10, not yet adopted. Followed once (#36 and I35) |
| 10 | Read the threat model and limitations against the code at each milestone | `docs/reviews/` | ☐ Not yet a habit. Done once on 2026-11-06, when two wrong claims were found |
