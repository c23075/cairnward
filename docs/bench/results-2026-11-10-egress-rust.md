# Egress data paths, Python against Rust, 2026-11-10 (ADR-0020, stage 5)

`scripts/bench_egress.py --requests 300 --clients 16`. The machine was the build container:
4 vCPUs (Intel Xeon, 2.1 GHz), the audit log on its virtual disk. Both data paths run against
the same local origins with the same policy engine, decision logic and audit log. Each request
uses a new connection. "direct" is the same request sent to the origin without a proxy.

Three runs, median / p95 in ms per request:

| Run | Path | direct | plain HTTP | CONNECT tunnel | TLS-inspected | req/s, 16 clients |
|---|---|---|---|---|---|---|
| 1 | Python | 6.90 / 10.40 | 23.53 / 32.77 | 8.70 / 10.93 | 68.69 / 78.92 | 58 |
| 1 | Rust | 6.45 / 10.05 | 23.73 / 31.97 | 9.06 / 11.22 | 68.07 / 76.79 | 57 |
| 2 | Python | 6.62 / 10.21 | 23.12 / 32.48 | 8.92 / 10.86 | 71.85 / 84.86 | 51 |
| 2 | Rust | 6.67 / 10.66 | 24.25 / 34.25 | 9.57 / 11.53 | 68.52 / 78.94 | 55 |
| 3 | Python | 6.97 / 11.33 | 24.30 / 33.17 | 9.22 / 11.58 | 71.69 / 80.71 | 57 |
| 3 | Rust | 6.79 / 10.49 | 23.86 / 34.61 | 9.25 / 11.53 | 67.90 / 76.35 | 59 |

**The two paths cost about the same.** Plain HTTP and tunnels are within a millisecond.
TLS-inspected requests are 1–4 ms faster in Rust. The overhead of a plain request over going
direct, about 17 ms, is mostly the two audit records per connection waiting for the disk, and
those stay in Python for both. Rust doesn't change that cost, by design.

## Found while measuring
- **Audit appends failed under load (fixed, I41).** With 8–16 clients at once, about 1
  connection in 300 was dropped without an answer on either path: an audit append failed with
  "database is locked". A slow `fsync` held SQLite's lock past the 5 s `busy_timeout`. The
  connection was never made, so this failed closed, but it was a real bug. Now appenders in
  one process queue on a lock with no time limit, and `busy_timeout` is 30 s for other
  processes. `tests/test_audit.py` reproduces it with a slow disk and a short timeout. Five
  more runs of 300 with 16 clients had no failures.
- **Rust was 38 ms slower on TLS-inspected requests at first (fixed).** Nagle's algorithm with
  delayed ACKs held back small TLS writes. asyncio sets `TCP_NODELAY`; the Rust path now does
  too.
- **The benchmark's own origin** (Python's `http.server`) had a listen backlog of 5. It's 256
  here, so the origin isn't what's measured.

## Not covered
- **Isolated mode**, a real network, or real sites: the origins are on this machine.
- **Long-lived connections and large bodies:** each request here is small and on a new
  connection.
- **The Strict overhead target (I14)** concerns the gateway, not egress. These numbers don't
  change it.
