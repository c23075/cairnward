# ADR-0016: Dashboard as plain ES modules, without a build step

- Status: Accepted (owner review 2026-11-02). Svelte was reconsidered: the framework itself is sound, but its npm build toolchain would put a supply chain into the page where approvals are given; revisit only if the dashboard grows a lot
- Date: 2026-11-01
- Spec: §3.1, §4.9; FR-DASH-1..3, FR-VLT-3; supersedes the "Svelte, built offline" line in §6

## Context
The spec chose Svelte, compiled offline and served by the core. The dashboard's job is small: a handful of tables, forms and dialogs over a local JSON API. What matters most for a privacy tool is that users can audit what runs in their browser and that the supply chain stays small.

## Decision
Plain JavaScript ES modules and one stylesheet, served as they are in the repository (`src/cairnward/dashboard/static/`): no npm dependencies, no bundler, no compiled output to trust. A small `h()` helper builds elements with `textContent` only; a test fails if any served file uses `innerHTML`, `outerHTML`, `insertAdjacentHTML`, `document.write` or `eval`. The page runs under a strict CSP (`script-src 'self'`, no inline code).

## Consequences
- What the browser runs is exactly what is in git, readable without tooling; Python-only installs need no Node.
- More hand-written DOM code than Svelte would need; acceptable at this size. Revisit (Svelte or similar, with committed lockfile and reproducible build) if the UI grows a lot.
- Browser tests use Playwright from a pinned lockfile under `tests/e2e/` (CI only).
