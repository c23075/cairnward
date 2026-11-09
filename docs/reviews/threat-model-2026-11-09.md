# Threat model review, 2026-11-09 (NFR-S2)

Covers PR 47: path-rule matching under TLS inspection (D78) and IPv6 host patterns. Neither
change adds a component, data flow or trust boundary; both tighten existing controls. Recorded
because the inspection rules are a control in T-17 and T-34, and the bypass was a gap in them.

**How it was reviewed:**
- An audit of egress, the policy engine, the dashboard and gateway sign-in, artifacts, the audit
  log and replication crypto, reading the code and probing the rule checker and `host_matches`
  directly with crafted inputs.
- Each finding got tests that fail without its fix and pass with it.
- Not a penetration test, and not run against real servers (Tomcat, Jetty, nginx, a CDN). The
  reviewer is the coding assistant that wrote the fixes, so an outside review is still
  outstanding (A10).

## What changed in the model
- **New threat T-37:** path confusion between egress's rule check and the server.
- **LLM02:** now lists T-37.
- No change to assets, trust boundaries or LINDDUN threats.

## Findings

| # | Finding | Severity | State |
|---|---|---|---|
| R1 | **Dot segments and escapes bypassed path rules (T-37).** Rules matched the raw path: `/repos/me/../../other` and `/repos/me/%2e%2e/%2e%2e/other` passed an allow rule for `/repos/me/**`; `//admin`, `/./admin`, `/x/../admin` and `/%61dmin` passed a deny rule for `/admin` | High when inspection is on (off by default) | **Fixed.** The path is normalized before matching (RFC 3986 §6.2.2: unreserved escapes decoded, `//`, `.`, `..` resolved, never above the root). Tests: `test_request_path_is_normalized`, `test_rules_see_the_path_the_server_will_resolve` |
| R2 | **Encoded separators can't be judged (T-37).** `%2f`, `%5c`, `%00` and `\` are read as separators or terminators by some servers and not others | Medium | **Fixed** by refusing such paths outright, whatever the rules say. Test: `test_rules_see_the_path_the_server_will_resolve` |
| R3 | **`;` parameters and letter case (T-37).** Tomcat, Jetty and Spring drop `;` parameters before resolving dot segments, so `/repos/me/..;/..;/other` is `/other` to them; servers that ignore case serve `/Admin` for `/admin`. R1's fix didn't cover these | High when inspection is on | **Fixed.** Each request is checked under four readings (as written, without `;` parameters, and both ignoring case) and goes out only if all are allowed. Lowercasing or stripping outright was rejected because it would wrongly allow or deny on servers that respect case and `;`. Cost: a legitimate request whose case or parameters change the matching rule is refused. Test: `test_case_and_path_parameters_cannot_steer_past_rules` |
| R4 | **IPv6 host patterns without a port never matched.** `::1` or `[::1]` in `network_allow_hosts`, `skip_hosts` or inspection rules didn't match egress's `[::1]:443` target | Low (fails closed: denied or asked) | **Fixed.** `host_matches` reads bracketed and bare IPv6 alike. Tests: new cases in `test_host_matches` |

## Checked and found sound
- **Egress connection handling:** proxy tokens compared in constant time; control characters in
  the request head refused; DNS resolved once and the checked address dialled; loopback,
  link-local, multicast and metadata addresses refused, including IPv4 inside IPv6 forms.
- **A trailing dot** (`example.com.`) doesn't match an allow-list entry, so it fails closed;
  there are no host deny lists it could slip past.
- **Gateway and dashboard:** loopback `Host` only; hashed client keys; single-use sign-in links,
  SameSite=Strict cookie, CSRF token and Origin check on changes; strict CSP; sandboxed previews.
- **Artifacts:** paths confined to the repository and out of `.git`; `rev` must be a commit id;
  tar extraction uses `filter="data"`.
- **Replication:** ChaCha20-Poly1305 with random nonces, Ed25519 signatures, records bound to
  their position by associated data.

## Not covered (residual risk, T-37)
- Real servers. The four readings model the common differentials; others aren't modelled:
  double decoding after routing, other matrix-parameter syntaxes, Unicode case folding,
  servers that don't collapse `//`. An allow list with `unmatched = "deny"` limits the damage.
- Request bodies and query strings are still not checked or redacted (T-17, I27).
