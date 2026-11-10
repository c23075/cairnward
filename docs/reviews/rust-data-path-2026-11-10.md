# Threat model review: the Rust egress data path, 2026-11-10 (NFR-S2)

Covers ADR-0020 stages 1–4: `cairnward-egress-rs` (the proxy, with TLS inspection), the decision
service it asks (`egress/decisions.py`), and `cairnward-audit-rs` (an independent verifier). The
data path is opt-in (`[egress] implementation = "rust"`). This review is part of deciding whether
it becomes the default (stage 5).

**What changed:** with the Rust path, egress is two processes. The Rust binary handles
connections, parsing, DNS, address checks and TLS. It asks the Python process for sessions,
policy, approvals, certificates, request verdicts and audit records over a Unix socket.

**How it was reviewed:**
- Every path into the decision service was traced, and who can reach its socket in each mode.
- Every egress, egress-fuzz and TLS-inspection test was run against both data paths with real
  sockets and real TLS, and `serve` was run with the Rust path in the real sandbox.
- Both paths were load-tested (`scripts/bench_egress.py`), which found R2 and R3.
- The reviewer is the coding assistant that wrote the code; an outside review is still
  outstanding (A10).

## What changed in the model
- **B10:** the Rust data path ↔ the decision service.
- **T-39:** misuse of the decision socket.
- **T-03:** notes the independent verifier.

## Findings

| # | Finding | Severity | State |
|---|---|---|---|
| R1 | **The sandboxed core could use the decision socket.** In isolated mode the core may connect to any Unix socket (`allow_all_unix_sockets`), and the socket was in the state folder. With a run's inspecting session, which the core creates through egress's control API, it could have asked `certificate` for the CA-signed certificate and key of any host that session may reach. The core could never obtain those before (review 2026-11-05, R5). It could also have written `net.connect` records on egress's behalf | High | **Fixed.** The service requires a random secret as the first line of every connection. `serve` hands the secret to the Rust binary on stdin, never in its environment or arguments. Tests: `test_with_a_secret_only_its_holder_is_answered`, and the Rust runner and isolation tests run with the secret |
| R2 | **Audit appends failed under load** with "database is locked", dropping the connection (on both data paths) | Medium (fails closed: nothing was sent unrecorded) | **Fixed (I41):** appenders in one process queue on a lock; `busy_timeout` 30 s across processes. Test: `test_many_appenders_wait_their_turn_on_a_slow_disk` |
| R3 | **TLS-inspected requests were 38 ms slower in Rust** (Nagle's algorithm with delayed ACKs) | Low (performance) | **Fixed:** `TCP_NODELAY` on both sides, as asyncio does |
| R4 | **The binary is found on `PATH`** if `CAIRNWARD_EGRESS_RS` isn't set and no repository build exists. A program planted there would become egress | Low, accepted | Planting it needs the user's own account, which owns Cairnward anyway. Set `CAIRNWARD_EGRESS_RS` to an absolute path. Pinning the binary by hash, as plugins are, is a possible improvement |
| R5 | **A panic ends the whole proxy** (release builds abort) | Low, accepted | Fails closed: nothing goes out until `serve` restarts. `unwrap` and `expect` are refused by clippy, and the fuzz tests run against the Rust path |
| R6 | **HTTP is parsed by hand,** not by a library like h11 | Medium, accepted | Every egress and fuzz test runs against both paths. One difference was found and fixed (an unclosed IPv6 bracket). Bodies are framed exactly, and anything ambiguous is refused |

## Checked and found sound
- **The CA key stays in Python.** Rust gets one host's certificate and key at a time, only for a
  session the policy marked for inspection.
- **The binary's environment** holds only `PATH`. The secret goes on stdin, and its arguments
  hold no secrets.
- **Limits:** request heads at 64 KiB, chunk lines bounded, connections capped
  (`max_connections`), and decision-service lines at 64 KiB.
- **TLS:** rustls's safe defaults, TLS 1.2 or later, ALPN `http/1.1` only, the upstream checked
  against Mozilla's roots with the host name verified.
- **The audit verifier** (`cairnward-audit-rs`) only reads. It agrees with Python on record
  hashes, the chain and Merkle roots over generated records, and catches the same tampering.

## Decision on the default
The Rust path stays opt-in, with `python` as the default. It now matches Python in every shared
test and in cost, and this review's findings are fixed. But ADR-0020 asks that it run for real,
not only in tests, before the default changes. That's an owner action (A18).

## Not covered
- **Real sites and a real network:** TLS to real sites, and real agents through the Rust path.
- **Long-running use:** memory over days, many simultaneous long connections.
