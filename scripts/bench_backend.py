#!/usr/bin/env python3
"""Measure local model-server speed for Cairnward's performance requirements.

Standard library only: run it with any Python 3.12+, no install needed.

    python scripts/bench_backend.py --model qwen3:30b
    python scripts/bench_backend.py --backend openai \
        --url http://127.0.0.1:8080/v1 --model qwen3-30b

It measures, for several prompt sizes:
  - time to first token (TTFT): what you wait before the reply starts
  - prompt processing speed (tokens/s): how fast the model reads your input
  - generation speed (tokens/s): how fast the reply is written
and, optionally, how much a reused prompt prefix (KV cache) speeds up a follow-up turn.

The prompts are generated filler text; no personal data is sent anywhere. Results are
printed and saved as JSON (commit chosen results to docs/bench/). See docs/bench/README.md.
"""

from __future__ import annotations

import argparse
import ctypes
import json
import os
import platform
import random
import statistics
import string
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from collections.abc import Iterator
from dataclasses import asdict, dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

DEFAULT_URLS = {"ollama": "http://127.0.0.1:11434", "openai": "http://127.0.0.1:8080/v1"}
DEFAULT_SIZES = [512, 2048, 8192, 16384, 30000]
INSTRUCTION = "\n\nIn one sentence, what is the text above about?"
LOOPBACK_HOSTS = {"127.0.0.1", "localhost", "::1"}

# ---------------------------------------------------------------------------
# Data


@dataclass
class RunResult:
    target_tokens: int
    prompt_tokens: int
    gen_tokens: int
    ttft_s: float
    total_s: float
    prompt_tps: float | None
    gen_tps: float | None
    load_s: float | None = None
    note: str = ""


@dataclass
class Report:
    started_at: str
    backend: str
    url: str
    model: str
    context: int
    gen_tokens: int
    think: bool | None
    system: dict[str, Any]
    warmup_load_s: float | None = None
    model_memory: dict[str, Any] | None = None
    tokens_per_char: float | None = None
    runs: list[RunResult] = field(default_factory=list)
    cache_test: dict[str, Any] | None = None
    summary: dict[str, Any] = field(default_factory=dict)


# ---------------------------------------------------------------------------
# System info


def _total_ram_bytes() -> int | None:
    try:
        if sys.platform.startswith("linux"):
            with Path("/proc/meminfo").open() as fh:
                for line in fh:
                    if line.startswith("MemTotal:"):
                        return int(line.split()[1]) * 1024
        if sys.platform == "win32":

            class MemStatus(ctypes.Structure):
                _fields_ = [
                    ("dwLength", ctypes.c_ulong),
                    ("dwMemoryLoad", ctypes.c_ulong),
                    ("ullTotalPhys", ctypes.c_ulonglong),
                    ("ullAvailPhys", ctypes.c_ulonglong),
                    ("ullTotalPageFile", ctypes.c_ulonglong),
                    ("ullAvailPageFile", ctypes.c_ulonglong),
                    ("ullTotalVirtual", ctypes.c_ulonglong),
                    ("ullAvailVirtual", ctypes.c_ulonglong),
                    ("ullAvailExtendedVirtual", ctypes.c_ulonglong),
                ]

            stat = MemStatus()
            stat.dwLength = ctypes.sizeof(MemStatus)
            if ctypes.windll.kernel32.GlobalMemoryStatusEx(ctypes.byref(stat)):  # type: ignore[attr-defined,unused-ignore]
                return int(stat.ullTotalPhys)
    except (OSError, ValueError, AttributeError):
        return None
    return None


def _cpu_model() -> str:
    try:
        if sys.platform.startswith("linux"):
            with Path("/proc/cpuinfo").open() as fh:
                for line in fh:
                    if line.startswith("model name"):
                        return line.split(":", 1)[1].strip()
    except OSError:
        pass
    return platform.processor() or platform.machine()


def system_info() -> dict[str, Any]:
    ram = _total_ram_bytes()
    return {
        "os": platform.platform(),
        "python": platform.python_version(),
        "cpu": _cpu_model(),
        "logical_cpus": os.cpu_count(),
        "ram_gb": round(ram / 2**30, 1) if ram else None,
    }


# ---------------------------------------------------------------------------
# Prompt generation


def filler_text(n_chars: int, rng: random.Random) -> str:
    """Readable-ish filler: random words in sentences, so tokenization is realistic."""
    words: list[str] = []
    length = 0
    while length < n_chars:
        word_len = rng.randint(2, 10)
        word = "".join(rng.choice(string.ascii_lowercase) for _ in range(word_len))
        words.append(word)
        length += word_len + 1
        if rng.random() < 0.08:
            words[-1] += "."
    return " ".join(words)[:n_chars]


def make_prompt(target_tokens: int, tokens_per_char: float, rng: random.Random) -> str:
    # A random nonce at the start defeats prefix (KV) cache reuse between runs.
    nonce = f"[run {rng.getrandbits(64):016x}] "
    n_chars = max(16, int(target_tokens / tokens_per_char))
    return nonce + filler_text(n_chars, rng) + INSTRUCTION


# ---------------------------------------------------------------------------
# HTTP


def _opener(url: str) -> urllib.request.OpenerDirector:
    host = urllib.parse.urlparse(url).hostname or ""
    if host in LOOPBACK_HOSTS:
        # Never route loopback traffic through a configured HTTP(S) proxy.
        return urllib.request.build_opener(urllib.request.ProxyHandler({}))
    return urllib.request.build_opener()


def _post_stream(
    url: str, body: dict[str, Any], headers: dict[str, str], timeout: float
) -> Iterator[bytes]:
    data = json.dumps(body).encode()
    req = urllib.request.Request(url, data=data, method="POST")
    req.add_header("Content-Type", "application/json")
    for key, value in headers.items():
        req.add_header(key, value)
    with _opener(url).open(req, timeout=timeout) as resp:
        yield from resp


def _get_json(url: str, timeout: float) -> Any:
    with _opener(url).open(url, timeout=timeout) as resp:
        return json.loads(resp.read())


# ---------------------------------------------------------------------------
# Backends


class Backend:
    name = ""

    def __init__(self, url: str, model: str, ctx: int, think: bool | None, timeout: float):
        self.url = url.rstrip("/")
        self.model = model
        self.ctx = ctx
        self.think = think
        self.timeout = timeout

    def chat(self, messages: list[dict[str, str]], gen: int, target: int) -> RunResult:
        raise NotImplementedError

    def model_memory(self) -> dict[str, Any] | None:
        return None


class OllamaBackend(Backend):
    name = "ollama"

    def chat(self, messages: list[dict[str, str]], gen: int, target: int) -> RunResult:
        body: dict[str, Any] = {
            "model": self.model,
            "messages": messages,
            "stream": True,
            "keep_alive": "15m",
            "options": {"num_ctx": self.ctx, "num_predict": gen, "temperature": 0, "seed": 1},
        }
        if self.think is not None:
            body["think"] = self.think
        start = time.perf_counter()
        first: float | None = None
        final: dict[str, Any] = {}
        for raw in _post_stream(f"{self.url}/api/chat", body, {}, self.timeout):
            line = raw.strip()
            if not line:
                continue
            chunk = json.loads(line)
            if "error" in chunk:
                raise RuntimeError(f"Ollama error: {chunk['error']}")
            msg = chunk.get("message") or {}
            if first is None and (msg.get("content") or msg.get("thinking")):
                first = time.perf_counter()
            if chunk.get("done"):
                final = chunk
        end = time.perf_counter()
        ns = 1e9
        p_count = int(final.get("prompt_eval_count", 0))
        p_dur = float(final.get("prompt_eval_duration", 0)) / ns
        e_count = int(final.get("eval_count", 0))
        e_dur = float(final.get("eval_duration", 0)) / ns
        return RunResult(
            target_tokens=target,
            prompt_tokens=p_count,
            gen_tokens=e_count,
            ttft_s=(first or end) - start,
            total_s=end - start,
            prompt_tps=p_count / p_dur if p_dur > 0 else None,
            gen_tps=e_count / e_dur if e_dur > 0 else None,
            load_s=float(final.get("load_duration", 0)) / ns,
        )

    def model_memory(self) -> dict[str, Any] | None:
        try:
            data = _get_json(f"{self.url}/api/ps", self.timeout)
        except (urllib.error.URLError, OSError, ValueError):
            return None
        for entry in data.get("models", []):
            if entry.get("name") == self.model or entry.get("model") == self.model:
                return {
                    "size_gb": round(entry.get("size", 0) / 2**30, 2),
                    "size_vram_gb": round(entry.get("size_vram", 0) / 2**30, 2),
                    "context_length": entry.get("context_length"),
                }
        return None


class OpenAIBackend(Backend):
    name = "openai"

    def __init__(self, *args: Any, api_key: str | None = None, **kwargs: Any):
        super().__init__(*args, **kwargs)
        self.api_key = api_key

    def chat(self, messages: list[dict[str, str]], gen: int, target: int) -> RunResult:
        if self.think is False:
            # Qwen3 hybrid models honour the /no_think soft switch.
            messages = [
                *messages[:-1],
                {**messages[-1], "content": messages[-1]["content"] + " /no_think"},
            ]
        body: dict[str, Any] = {
            "model": self.model,
            "messages": messages,
            "stream": True,
            "stream_options": {"include_usage": True},
            "max_tokens": gen,
            "temperature": 0,
            "seed": 1,
        }
        headers = {"Authorization": f"Bearer {self.api_key}"} if self.api_key else {}
        start = time.perf_counter()
        first: float | None = None
        usage: dict[str, Any] = {}
        timings: dict[str, Any] = {}
        for raw in _post_stream(f"{self.url}/chat/completions", body, headers, self.timeout):
            line = raw.strip()
            if not line.startswith(b"data:"):
                continue
            payload = line[5:].strip()
            if payload == b"[DONE]":
                break
            chunk = json.loads(payload)
            for choice in chunk.get("choices") or []:
                delta = choice.get("delta") or {}
                if first is None and (delta.get("content") or delta.get("reasoning_content")):
                    first = time.perf_counter()
            if chunk.get("usage"):
                usage = chunk["usage"]
            if chunk.get("timings"):  # llama.cpp llama-server extension
                timings = chunk["timings"]
        end = time.perf_counter()
        ttft = (first or end) - start
        if timings:
            p_count = int(timings.get("prompt_n", 0))
            p_ms = float(timings.get("prompt_ms", 0))
            e_count = int(timings.get("predicted_n", 0))
            e_ms = float(timings.get("predicted_ms", 0))
            prompt_tps = p_count / (p_ms / 1000) if p_ms > 0 else None
            gen_tps = e_count / (e_ms / 1000) if e_ms > 0 else None
            note = "server timings"
        else:
            p_count = int(usage.get("prompt_tokens", 0))
            e_count = int(usage.get("completion_tokens", 0))
            prompt_tps = p_count / ttft if ttft > 0 and p_count else None
            gen_time = end - (first or end)
            gen_tps = (e_count - 1) / gen_time if gen_time > 0 and e_count > 1 else None
            note = "estimated from wall clock (includes network latency)"
        return RunResult(
            target_tokens=target,
            prompt_tokens=p_count,
            gen_tokens=e_count,
            ttft_s=ttft,
            total_s=end - start,
            prompt_tps=prompt_tps,
            gen_tps=gen_tps,
            note=note,
        )


# ---------------------------------------------------------------------------
# Benchmark


def calibrate(backend: Backend, rng: random.Random) -> float:
    """Measure tokens per character of the filler text with a small request."""
    probe_chars = 2000
    text = filler_text(probe_chars, rng) + INSTRUCTION
    result = backend.chat([{"role": "user", "content": text}], gen=1, target=0)
    if result.prompt_tokens <= 0:
        return 0.3  # typical for random lowercase words
    return result.prompt_tokens / len(text)


def cache_test(
    backend: Backend, size: int, gen: int, tpc: float, rng: random.Random
) -> dict[str, Any]:
    """Second turn reusing the first turn's prefix: shows the value of KV-cache reuse."""
    prompt = make_prompt(size, tpc, rng)
    first_msgs = [{"role": "user", "content": prompt}]
    cold = backend.chat(first_msgs, gen, size)
    follow = [
        *first_msgs,
        {"role": "assistant", "content": "It is filler text."},
        {"role": "user", "content": "Give one more word that could appear in it."},
    ]
    warm = backend.chat(follow, gen, size)
    return {
        "size": size,
        "cold_ttft_s": round(cold.ttft_s, 2),
        "followup_ttft_s": round(warm.ttft_s, 2),
        "speedup": round(cold.ttft_s / warm.ttft_s, 1) if warm.ttft_s > 0 else None,
    }


def summarise(report: Report) -> dict[str, Any]:
    by_size: dict[int, list[RunResult]] = {}
    for run in report.runs:
        by_size.setdefault(run.target_tokens, []).append(run)
    table = {}
    for size, runs in sorted(by_size.items()):
        p = [r.prompt_tps for r in runs if r.prompt_tps]
        g = [r.gen_tps for r in runs if r.gen_tps]
        table[str(size)] = {
            "prompt_tokens": int(statistics.median(r.prompt_tokens for r in runs)),
            "ttft_s": round(statistics.median(r.ttft_s for r in runs), 2),
            "prompt_tps": round(statistics.median(p), 1) if p else None,
            "gen_tps": round(statistics.median(g), 1) if g else None,
        }
    all_p = [r.prompt_tps for r in report.runs if r.prompt_tps]
    all_g = [r.gen_tps for r in report.runs if r.gen_tps]
    summary: dict[str, Any] = {"by_size": table}
    if all_p:
        p_med = statistics.median(all_p)
        summary["prompt_tps_median"] = round(p_med, 1)
        summary["est_seconds_to_read_32k_prompt"] = round(32768 / p_med, 0)
        summary["max_prompt_tokens_within_60s"] = int(p_med * 60)
    if all_g:
        summary["gen_tps_median"] = round(statistics.median(all_g), 1)
    return summary


def print_report(report: Report) -> None:
    print()
    print(f"Model: {report.model}  Backend: {report.backend} ({report.url})")
    print(f"Context: {report.context}")
    sysinfo = report.system
    print(f"System: {sysinfo['cpu']} | {sysinfo['logical_cpus']} logical CPUs")
    print(f"        {sysinfo['ram_gb']} GB RAM | {sysinfo['os']}")
    if report.warmup_load_s is not None:
        print(f"Model load time (warm-up): {report.warmup_load_s:.1f} s")
    if report.model_memory:
        print(f"Model memory (server-reported): {report.model_memory}")
    print()
    header = (
        f"{'target':>7} {'prompt tok':>10} {'TTFT s':>8} {'prompt tok/s':>13} {'gen tok/s':>10}"
    )
    print(header)
    print("-" * len(header))
    for size, row in report.summary["by_size"].items():
        p = row["prompt_tps"] if row["prompt_tps"] is not None else "-"
        g = row["gen_tps"] if row["gen_tps"] is not None else "-"
        print(f"{size:>7} {row['prompt_tokens']:>10} {row['ttft_s']:>8} {p!s:>13} {g!s:>10}")
    print()
    s = report.summary
    if "prompt_tps_median" in s:
        print(
            f"Median prompt speed: {s['prompt_tps_median']} tok/s "
            f"-> a full 32K prompt takes ~{s['est_seconds_to_read_32k_prompt']:.0f} s to read; "
            f"~{s['max_prompt_tokens_within_60s']} tokens fit in 60 s (NFR-P4 threshold)"
        )
    if "gen_tps_median" in s:
        print(f"Median generation speed: {s['gen_tps_median']} tok/s")
    if report.cache_test:
        c = report.cache_test
        print(
            f"Prefix-cache test at {c['size']} tokens: first turn TTFT {c['cold_ttft_s']} s, "
            f"follow-up {c['followup_ttft_s']} s (x{c['speedup']})"
        )


def parse_args(argv: list[str] | None) -> argparse.Namespace:
    ap = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    ap.add_argument("--backend", choices=["ollama", "openai"], default="ollama")
    ap.add_argument("--url", help="server URL (default: local Ollama or llama-server)")
    ap.add_argument(
        "--model", required=True, help="model name as the server knows it, e.g. qwen3:30b"
    )
    ap.add_argument(
        "--sizes",
        default=",".join(map(str, DEFAULT_SIZES)),
        help="comma-separated prompt sizes in tokens",
    )
    ap.add_argument(
        "--ctx", type=int, default=32768, help="context window to request (Ollama num_ctx)"
    )
    ap.add_argument("--gen", type=int, default=128, help="tokens to generate per run")
    ap.add_argument("--runs", type=int, default=2, help="runs per size (median is reported)")
    think = ap.add_mutually_exclusive_group()
    think.add_argument(
        "--no-think",
        dest="think",
        action="store_false",
        default=None,
        help="disable Qwen3 thinking mode (recommended for speed numbers)",
    )
    think.add_argument("--think", dest="think", action="store_true")
    ap.add_argument("--cache-test", action="store_true", help="also measure prefix-cache reuse")
    ap.add_argument(
        "--api-key",
        default=os.environ.get("CAIRN_BENCH_API_KEY"),
        help="API key for OpenAI-compatible servers (or env CAIRN_BENCH_API_KEY)",
    )
    ap.add_argument("--timeout", type=float, default=1800, help="seconds per request")
    ap.add_argument("--out", help="JSON output path (default: bench-<model>-<time>.json)")
    ap.add_argument("--seed", type=int, default=None, help="random seed for filler text")
    return ap.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    url = args.url or DEFAULT_URLS[args.backend]
    host = urllib.parse.urlparse(url).hostname or ""
    if host not in LOOPBACK_HOSTS:
        print(
            f"Note: {host} is not on this machine. Only generated filler text is sent.",
            file=sys.stderr,
        )
    sizes = sorted({int(s) for s in args.sizes.split(",") if s.strip()})
    usable = [s for s in sizes if s + args.gen + 256 <= args.ctx]
    skipped = sorted(set(sizes) - set(usable))
    if skipped:
        print(
            f"Skipping sizes {skipped}: they don't fit in --ctx {args.ctx} with --gen {args.gen}.",
            file=sys.stderr,
        )

    backend: Backend
    if args.backend == "ollama":
        backend = OllamaBackend(url, args.model, args.ctx, args.think, args.timeout)
    else:
        backend = OpenAIBackend(
            url, args.model, args.ctx, args.think, args.timeout, api_key=args.api_key
        )

    rng = random.Random(args.seed)  # noqa: S311 - not used for security
    report = Report(
        started_at=datetime.now(UTC).isoformat(timespec="seconds"),
        backend=args.backend,
        url=url,
        model=args.model,
        context=args.ctx,
        gen_tokens=args.gen,
        think=args.think,
        system=system_info(),
    )

    try:
        print("Warm-up (loads the model if needed)...", file=sys.stderr)
        warm = backend.chat([{"role": "user", "content": "Say OK."}], gen=4, target=0)
        report.warmup_load_s = warm.load_s if warm.load_s is not None else warm.total_s
        report.model_memory = backend.model_memory()
        tpc = calibrate(backend, rng)
        report.tokens_per_char = round(tpc, 4)
        for size in usable:
            for i in range(args.runs):
                print(f"  size {size}, run {i + 1}/{args.runs}...", file=sys.stderr)
                prompt = make_prompt(size, tpc, rng)
                report.runs.append(
                    backend.chat([{"role": "user", "content": prompt}], args.gen, size)
                )
        if args.cache_test and usable:
            size = (
                max(s for s in usable if s <= 8192) if any(s <= 8192 for s in usable) else usable[0]
            )
            print(f"  prefix-cache test at {size}...", file=sys.stderr)
            report.cache_test = cache_test(backend, size, args.gen, tpc, rng)
    except urllib.error.URLError as exc:
        print(f"Could not reach {url}: {exc.reason}. Is the server running?", file=sys.stderr)
        return 2
    except (RuntimeError, urllib.error.HTTPError) as exc:
        print(f"Server error: {exc}", file=sys.stderr)
        return 2
    except KeyboardInterrupt:
        print("Interrupted; saving partial results.", file=sys.stderr)

    report.summary = summarise(report)
    print_report(report)
    stamp = datetime.now(UTC).strftime("%Y%m%dT%H%M%SZ")
    safe_model = "".join(c if c.isalnum() or c in "-._" else "_" for c in args.model)
    out = Path(args.out or f"bench-{safe_model}-{stamp}.json")
    payload = asdict(report)
    out.write_text(json.dumps(payload, indent=2) + "\n")
    print(f"\nSaved {out}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
