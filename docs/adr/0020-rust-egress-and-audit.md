# ADR-0020: Rewriting the egress proxy and audit log in Rust

- Status: Accepted 2026-11-10 (owner go-ahead, A17); stages 1 and 2 done (opt-in), stage 3 next
- Date: 2026-11-10
- Spec: §3.2, §5.2, §6, §9, D9; follows [ADR-0003](0003-python-now-rust-later.md)

## Context
ADR-0003 chose Python first and named the egress proxy and audit log as the first parts to move
to Rust. They're the most exposed parts:
- **Egress** parses untrusted HTTP from every sandboxed tool and is the only way out.
- **The audit log** is the evidence everything else relies on.

In Python both carry a large dependency tree, which ADR-0003 named as the main audit cost. This session's audit also found a parsing bug in egress (I36).

Egress today isn't a separable proxy. One Python process holds all of these, and calls between
them are in-process:
- the proxy, with TLS inspection;
- the session tokens;
- the **policy engine and approver** (`PolicyEngine.check` runs in a thread from the proxy);
- the audit writer;
- in isolated mode, a control API on a Unix socket (`egress/control.py`) that the sandboxed core
  uses to create sessions, start MCP servers, run backups and edit settings.

## Options
1. **Rewrite everything in the egress process in Rust.** Policy, approvals and config loading
   would exist twice, in Python for the core and in Rust here, and drift apart.
2. **Split the data path from the decisions.** A Rust proxy does connection handling, header
   parsing, the address checks, TLS inspection and request rules. It asks a Python policy
   service over a Unix socket for each new connection's decision (`net.connect`), and gets back
   allow or deny plus the audit sequence number. Python keeps policy, approvals, config and the
   control API.
3. **Rust in-process through PyO3:** smaller step, but a parser bug in Rust still runs inside
   the Python process, and the deployment gains a native extension per platform.
4. **Don't rewrite; harden the Python** (fuzzing, property tests, fewer dependencies): cheapest,
   but leaves the dependency-tree concern in place.

For the audit log, the same split applies:
- **A small Rust audit writer** owns `audit.db` and the chain, and appends records sent to it
  over a socket. It would also take over the hash chain and Merkle checkpoints (D79), which are
  short, well specified and easy to test against the Python ones.
- **Python** would only send records.

## Decision
Option 2, in stages, with each stage shippable and the Python implementation kept as the
reference until the Rust one passes the same tests:
1. **Differential test harness.** Run the existing egress tests (`tests/test_egress*.py`,
   `tests/test_tls_inspection.py`) against any proxy binary through its network interface, so
   both implementations face the same cases.
2. **Rust egress data path** (`cairnward-egress-rs`, a separate binary and crate in this
   repository):
   - CONNECT and absolute-form HTTP;
   - control-character and header-size limits;
   - DNS resolved once, with the special-address checks (`egress/addresses.py`);
   - byte counts and timeouts;
   - asking a Python decision socket for allow or deny.
   - Opt-in with `[egress] implementation = "rust"`, Python the default.
3. **TLS inspection** in the Rust proxy: rustls, the path normalisation and readings from I36,
   and fronting checks.
4. **Rust audit writer**: chain, mirror file, Merkle roots; `audit verify` in both languages
   must agree on every record.
5. **Switch the default** once the Rust paths have run for real (not only against stand-ins),
   and drop the Python data path.

## Consequences
- **CI and dependencies:**
  - CI gains a Rust toolchain, `cargo test`, `cargo audit` and a pinned `Cargo.lock` (the
    hash-pinning convention).
  - Release builds need binaries for Linux, and for Windows through WSL.
- **Two implementations** exist until stage 5; the harness keeps them equal.
- **Decision round trip:** each new connection now crosses a socket to Python. This must be
  measured against the Strict overhead target (I14), and per-connection decisions cached the
  way approvals already are.
- **Unchanged:** the threat model's boundaries. The data path moves process; trust doesn't.
  Each stage still needs a threat-model review (NFR-S2).
- **To decide before stage 2:** the owner's go-ahead for a Rust toolchain in CI and releases.

## Progress
- **Stage 1, started 2026-11-10:**
  - **The Rust workspace:** `rust/`, crate `cairnward-egress`, binary `cairnward-egress-rs`,
    with `Cargo.lock` committed. It has no third-party dependencies yet, and forbids `unsafe`.
  - **Address checks:** `special_reason`, ported from `egress/addresses.py` with the same
    reasons.
  - **Differential test:** `tests/test_egress_rust.py` feeds 12,000 generated addresses to both
    implementations and requires identical answers. The addresses are random IPv4 and IPv6,
    special ranges, and special IPv4 inside IPv4-mapped, 6to4 and NAT64 forms. The test was
    checked to fail when the Rust 6to4 rule is broken.
  - **CI job `rust`:** a pinned toolchain (1.97.0), `cargo fmt --check`, `clippy -D warnings`
    (pedantic), `cargo test`, then the differential test.
  - **The decision service** (`egress/decisions.py`): the protocol the Rust data path will use
    to ask Python for policy, approvals and audit records. It's JSON lines over a 0600 Unix
    socket, with tickets per connection: `connect`, `allow_special`, `deny`, `opened`,
    `closed`. The answers come from the Python proxy's own methods, and the three-way connect
    decision moved into one shared method, `EgressProxy.connect_decision`, so neither path can
    drift. Tested over a real socket (`tests/test_egress_decisions.py`).
- **Stage 2, done 2026-11-10 (opt-in):**
  - **The proxy** (`cairnward-egress-rs serve`): CONNECT and absolute-form HTTP. It checks heads
    as the Python path does: control characters, a 64 KiB limit, the request line and headers.
    It resolves once, checks special addresses and dials what it checked. Plain HTTP is one
    request per connection, with `Host` from the checked URL and two `Host` headers refused.
    Bodies are framed exactly (Content-Length or chunked), so nothing after a request's body is
    forwarded. Byte counts, connect and idle timeouts, and a connection limit are kept.
  - **Dependencies:** only `tokio` and `serde_json`, with HTTP parsed by hand.
  - **Decisions:** the proxy asks the decision service for each connection over its own socket
    connection. The protocol gained a `session` operation, so a client without a valid token
    gets 407 before anything else is looked at, and a `status` field on `closed`.
  - **`[egress] implementation = "rust"`** (default `python`): `egress/rust.py` runs the binary
    and the decision service behind `EgressProxy`'s interface, for `serve` in either mode and for
    `cairnward egress`. Sessions that should be TLS-inspected are refused, not tunnelled unread,
    until stage 3, and `serve` warns when that applies.
  - **The shared fixture:** every test in `tests/test_egress.py` and the egress fuzz tests in
    `tests/test_fuzz.py` now runs against both data paths. This found a difference that's now
    fixed: an unclosed IPv6 bracket in a URL got 403 from Rust instead of Python's 400. Tests
    that inject DNS answers run against Python only, and a test that uses the real resolver
    covers both. `serve` with the Rust path is tested end to end with the real sandbox, both
    beside the isolated core and in a single process.
  - **Still to do before it can become the default (stage 5):**
    - bytes are counted raw (with chunk framing) rather than as decoded body bytes;
    - a request's 100-continue is answered locally, as h11 does;
    - not yet measured against the Strict overhead target (I14);
    - not yet run for real outside tests.
- **Next, stage 3:** TLS inspection in Rust (rustls), with the path readings from I36.
- **Dependency audit (2026-11-10):** CI runs `cargo audit --deny warnings` (cargo-audit 0.22.2,
  installed `--locked`) against the RustSec advisory database on every change. That fails on
  known vulnerabilities, unmaintained crates and yanked versions. Dependabot watches
  `rust/Cargo.lock`. The first run was clean: 24 crates, no advisories.
