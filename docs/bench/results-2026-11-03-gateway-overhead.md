# Gateway overhead, 2026-11-03 (NFR-P2)

`scripts/bench_gateway.py --requests 200 --dir <folder on the data disk>`: AMD Ryzen 3 3200G,
ext4, a real gateway and a stand-in model server on real local sockets. Three runs; model time
is excluded by subtracting the same request sent straight to the stand-in.

| Mode | Target | Median overhead | p95 | Result |
|---|---|---|---|---|
| Strict | ≤ 50 ms | 58–60 ms | 64–66 ms | **misses** |
| All protections off | ≤ 10 ms | 6–7 ms | 9 ms | meets |

**Strict's cost is mostly waiting for the disk.** The same benchmark with the data on a
memory-backed folder (`/tmp` here) gives 17 ms, about 42 ms less. That folder hides the miss,
so use `--dir` on the data disk. Which writes take the most wasn't measured separately; with
Strict on, each request writes two audit records and a vault entry, and the audit log waits
for the disk (`fsync`) before the request goes on (NFR-R2).

In context: a model call on this machine takes minutes, so 9 ms over target can't be noticed
in use. Making the writes wait less would weaken the guarantee that the record exists before
the effect. No such change was made.

## Not covered
- **Isolated mode:** requests here reach the gateway over TCP directly. With the core isolated
  they also pass through a local proxy into the sandbox, which isn't measured.
- **Redaction:** the backend is local, so outbound scanning doesn't run. A hosted backend adds it.
- **Size:** a short prompt on a new vault and audit log, without streaming. Real conversations
  are longer.
- Each request also makes one extra call to the model server's `/api/ps` for the memory check.
  It's included here, but this stand-in answers at once and real Ollama may be slower.

## An artefact to avoid
The first version of the stand-in sent its headers and body as two writes, which added about
50 ms to every mode through TCP's delayed ACK. It now sends them as one.
