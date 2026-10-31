"""Run scripts/bench_backend.py against fake Ollama and OpenAI-compatible servers."""

from __future__ import annotations

import importlib.util
import json
import sys
import threading
from collections.abc import Iterator
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from types import ModuleType
from typing import Any

import pytest

SCRIPT = Path(__file__).resolve().parents[1] / "scripts" / "bench_backend.py"


def load_bench() -> ModuleType:
    spec = importlib.util.spec_from_file_location("bench_backend", SCRIPT)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module  # dataclasses need the module registered
    spec.loader.exec_module(module)
    return module


bench = load_bench()


def _prompt_tokens(body: dict[str, Any]) -> int:
    return sum(len(m["content"]) for m in body["messages"]) // 4


class FakeHandler(BaseHTTPRequestHandler):
    seen: list[dict[str, Any]] = []  # noqa: RUF012 - shared across requests on purpose

    def log_message(self, format: str, *args: Any) -> None:
        pass

    def _body(self) -> dict[str, Any]:
        length = int(self.headers.get("Content-Length", 0))
        body: dict[str, Any] = json.loads(self.rfile.read(length))
        FakeHandler.seen.append(body)
        return body

    def do_GET(self) -> None:
        if self.path == "/api/ps":
            data = {
                "models": [
                    {"name": "fake:1b", "size": 2**31, "size_vram": 0, "context_length": 4096}
                ]
            }
            raw = json.dumps(data).encode()
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(raw)))
            self.end_headers()
            self.wfile.write(raw)
        else:
            self.send_error(404)

    def do_POST(self) -> None:
        body = self._body()
        n_prompt = _prompt_tokens(body)
        if self.path == "/api/chat":
            self.send_response(200)
            self.send_header("Content-Type", "application/x-ndjson")
            self.end_headers()
            for word in ["Hello", " world"]:
                self.wfile.write(
                    json.dumps({"message": {"content": word}, "done": False}).encode() + b"\n"
                )
            final = {
                "message": {"content": ""},
                "done": True,
                "prompt_eval_count": n_prompt,
                "prompt_eval_duration": 500_000_000,
                "eval_count": 2,
                "eval_duration": 100_000_000,
                "load_duration": 1_000_000,
            }
            self.wfile.write(json.dumps(final).encode() + b"\n")
        elif self.path == "/v1/chat/completions":
            self.send_response(200)
            self.send_header("Content-Type", "text/event-stream")
            self.end_headers()
            for word in ["Hello", " world"]:
                chunk = {"choices": [{"delta": {"content": word}}]}
                self.wfile.write(b"data: " + json.dumps(chunk).encode() + b"\n\n")
            final = {
                "choices": [],
                "usage": {"prompt_tokens": n_prompt, "completion_tokens": 2},
                "timings": {
                    "prompt_n": n_prompt,
                    "prompt_ms": 500.0,
                    "predicted_n": 2,
                    "predicted_ms": 100.0,
                },
            }
            self.wfile.write(b"data: " + json.dumps(final).encode() + b"\n\n")
            self.wfile.write(b"data: [DONE]\n\n")
        else:
            self.send_error(404)


@pytest.fixture
def server() -> Iterator[str]:
    FakeHandler.seen = []
    httpd = ThreadingHTTPServer(("127.0.0.1", 0), FakeHandler)
    thread = threading.Thread(target=httpd.serve_forever, daemon=True)
    thread.start()
    try:
        yield f"http://127.0.0.1:{httpd.server_address[1]}"
    finally:
        httpd.shutdown()


def test_ollama_run(server: str, tmp_path: Path) -> None:
    out = tmp_path / "r.json"
    code = bench.main(
        [
            "--model",
            "fake:1b",
            "--url",
            server,
            "--sizes",
            "256,1024",
            "--ctx",
            "4096",
            "--gen",
            "8",
            "--runs",
            "1",
            "--no-think",
            "--cache-test",
            "--out",
            str(out),
            "--seed",
            "1",
        ]
    )
    assert code == 0
    report = json.loads(out.read_text())
    assert [r["target_tokens"] for r in report["runs"]] == [256, 1024]
    # Calibration should land prompts near the requested size (fake counts chars/4).
    for run in report["runs"]:
        assert 0.8 * run["target_tokens"] <= run["prompt_tokens"] <= 1.2 * run["target_tokens"] + 20
    assert report["summary"]["gen_tps_median"] == 20.0
    assert report["model_memory"]["size_gb"] == 2.0
    assert report["cache_test"]["size"] == 1024
    # --no-think is passed to Ollama; num_ctx is requested.
    chat_bodies = [b for b in FakeHandler.seen if "messages" in b]
    assert all(b["think"] is False for b in chat_bodies)
    assert all(b["options"]["num_ctx"] == 4096 for b in chat_bodies)


def test_openai_run_uses_server_timings(server: str, tmp_path: Path) -> None:
    out = tmp_path / "r.json"
    code = bench.main(
        [
            "--backend",
            "openai",
            "--model",
            "fake",
            "--url",
            f"{server}/v1",
            "--sizes",
            "512",
            "--ctx",
            "4096",
            "--gen",
            "8",
            "--runs",
            "2",
            "--out",
            str(out),
        ]
    )
    assert code == 0
    report = json.loads(out.read_text())
    assert len(report["runs"]) == 2
    assert all(r["note"] == "server timings" for r in report["runs"])
    assert report["summary"]["gen_tps_median"] == 20.0


def test_sizes_that_do_not_fit_are_skipped(server: str, tmp_path: Path) -> None:
    out = tmp_path / "r.json"
    code = bench.main(
        [
            "--model",
            "fake:1b",
            "--url",
            server,
            "--sizes",
            "512,8000",
            "--ctx",
            "4096",
            "--gen",
            "8",
            "--runs",
            "1",
            "--out",
            str(out),
        ]
    )
    assert code == 0
    assert [r["target_tokens"] for r in json.loads(out.read_text())["runs"]] == [512]


def test_unreachable_server_returns_2(tmp_path: Path) -> None:
    code = bench.main(
        [
            "--model",
            "x",
            "--url",
            "http://127.0.0.1:9",
            "--sizes",
            "512",
            "--runs",
            "1",
            "--out",
            str(tmp_path / "r.json"),
            "--timeout",
            "2",
        ]
    )
    assert code == 2


def test_prompt_nonce_differs_between_runs() -> None:
    import random

    rng = random.Random(0)  # noqa: S311 - filler text only
    a = bench.make_prompt(100, 0.3, rng)
    b = bench.make_prompt(100, 0.3, rng)
    assert a[:30] != b[:30]
