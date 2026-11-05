# Benchmarks

`uv run cairnward check` runs everything that needs the real machine (doctor, the coding eval, client usage) and writes `check-<date>.md` here. Those reports are ignored on `main` and kept on the `dev` branch; hand-written `results-*.md` summaries stay here.

`scripts/bench_backend.py` measures how fast a model server runs on your machine. The results set Cairnward's performance requirements (NFR-P3, NFR-P4 in [requirements.md](../requirements.md)), e.g. when to warn that a prompt will take more than a minute to read.

It uses only the Python standard library, sends only generated filler text, and by default talks only to servers on this machine.

## Run it

With Ollama (default `http://127.0.0.1:11434`):

```bash
python3 scripts/bench_backend.py --model qwen3:30b --no-think --cache-test
```

Also measure the fast model:

```bash
python3 scripts/bench_backend.py --model qwen3:4b --no-think
```

With llama.cpp's `llama-server` (default `http://127.0.0.1:8080/v1`):

```bash
python3 scripts/bench_backend.py --backend openai --model qwen3-30b --no-think --cache-test
```

Options: `--sizes 512,2048,8192,16384,30000` (prompt sizes in tokens), `--ctx 32768`, `--gen 128`, `--runs 2`, `--out file.json`. Expect the full default run to take roughly 10–30 minutes on a CPU-only machine; the largest sizes take longest. Close other heavy apps while it runs.

## What the numbers mean

| Column | Meaning |
|---|---|
| TTFT s | Time to first token: how long you wait before the reply starts |
| prompt tok/s | How fast the model reads input. On CPU this dominates waiting time for long prompts |
| gen tok/s | How fast the reply is written |
| Prefix-cache test | How much faster a follow-up turn is when the server reuses the earlier prompt (why stable prompts matter on CPU) |

## Recording results

Copy the JSON into this folder as `<machine>-<model>-<date>.json` (e.g. `ref32gb-qwen3-30b-2026-11-01.json`) and commit it. The spec's provisional targets are then replaced with measured ones.
