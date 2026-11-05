# Threat model review, 2026-11-05 (NFR-S2)

Covers ADR-0018: TLS inspection in egress (D78) and OpenShell as a second sandbox (D77), merged in
PR 28 without this review (status.md, I32).

**How it was reviewed:**
- Each new component and data flow was traced in the code. Every egress session in the code was
  listed and checked against what the inspection code assumes.
- Each finding that could be tested got a test that fails without its fix and passes with it.
- It wasn't a penetration test. The reviewer is the coding assistant that wrote the code, so an
  outside review is still outstanding (A10).

## What changed in the model
- **Trust boundaries:** B8 (the core and OpenShell) and B9 (egress and the tools that trust its CA).
- **T-17:** updated for inspection. Bodies are still not redacted.
- **New threats:**
  - T-33: the CA key.
  - T-34: domain fronting.
  - T-35: plaintext in egress and per-request audit records.
  - T-36: the OpenShell boundary.
- **P-03, LLM02, LLM03 and the accepted risks:** updated to match.

## Findings

| # | Finding | Severity | State |
|---|---|---|---|
| R1 | **Inspection broke HTTPS for programs that weren't given the CA.** Egress decided by environment alone, but only `cairnward run` gives tools the CA bundle. Five other users of egress share the environment and would have failed TLS in an inspecting environment: MCP servers, restic to a remote repository, `mcp login`, plugin commands and model downloads. For restic that means remote backups failing. `/v1/runs` also serves the helper programs, so "is a run" wasn't enough to tell them apart | High (silent loss of backups for anyone turning inspection on) | **Fixed.** Sessions carry an `inspect` flag. Only `cairnward run` sets it (`tls_trust`), only when it gave the agent the CA, and egress honours it only in an inspecting environment. It's passed through the isolated-mode control API. Tests: `test_programs_without_the_ca_are_tunnelled_even_in_an_inspecting_environment`, `test_only_runs_given_the_ca_are_inspected`, `test_isolated_mode_passes_the_flag_to_egress` |
| R2 | **Domain fronting through an inspected tunnel (T-34).** The rules matched the host the tunnel was opened to, never the `Host` header. A request through a tunnel to an allowed CDN host could name another site the CDN serves | Medium | **Fixed** for inspected tunnels: a `Host` or absolute URL naming another site is refused with 403 and audited. Tests: `test_a_request_for_another_site_is_refused`, `test_fronting_check`. Uninspected tunnels can't be checked, which is a residual risk |
| R3 | **The CA key (T-33).** It's readable by the agent when isolation is off, and by any program running as the user. It has no name constraints, since the hosts are arbitrary | Medium, accepted | Documented. Off by default, never installed system-wide, rotation audited |
| R4 | **Path digests can be guessed (T-35).** A digest of `/repos/me/x` can be reversed by trying likely paths | Low, accepted | Documented. The same holds for other digests in the audit |
| R5 | **The isolated core could ask egress for `any_environment` sessions** if the control API accepted the field. While fixing R1, adding it to the API was considered and rejected: the control API has never accepted it, and the core is the less trusted side in isolated mode (T-32) | Avoided | The API accepts only the new `inspect` flag, which can make egress decrypt a session but never gives the core the plaintext |
| R6 | **OpenShell is trusted, and unverified (T-36).** These parts are outside Cairnward's control: its sandbox enforcement, the image it pulls (outside egress), its telemetry (on by default), and copies of the workspace in its storage | Medium, accepted | Opt-in, and the run warns about telemetry. The copy back is guarded against symlinked folders. The first real run is A11 |

## Checked and found sound
- **The CA key stays hidden.** srt runs hide the data folder (`run/runner.py`, `deny` list), so
  the CA key isn't readable in a sandboxed run. OpenShell sandboxes have none of the host's
  files.
- **The core is never inspected.** The core's own sessions are `trusted_core`, and inspection
  skips them.
- **The site's certificate is verified.** Egress checks the upstream site against public roots
  with hostname checking and TLS 1.2 or later. A bad certificate is a 502 to the tool and an
  audited error (`test_origin_certificate_is_verified`).
- **The run key stays off command lines.** With OpenShell, it travels in an uploaded env file,
  not in arguments (`test_run_in_openshell_copies_work_back_and_cleans_up` checks every recorded
  command line).
- **A kept sandbox doesn't keep a working key.** When a sandbox is kept after a failed copy back,
  its env file still holds the run key. The key is revoked when the run ends, so the file holds a
  dead key.

## Not covered
- A real OpenShell gateway, real agents, and outside sites through inspection. These are A11 and
  A12 in status.md.
- Whether MCP servers' sandboxes hide the data folder. The CA key's protection was only checked
  for `cairnward run`.
