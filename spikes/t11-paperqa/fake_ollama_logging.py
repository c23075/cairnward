#!/usr/bin/env python3
"""Instrumented stand-in for Ollama: logs the size of every call to calls.jsonl.

Answers evidence-summary prompts (which ask for JSON with a relevance score) with valid
JSON, other prompts with short text, embeddings with deterministic vectors.
Usage: fake_ollama_logging.py PORT LOGFILE
"""

import hashlib
import json
import sys
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

PORT, LOG = int(sys.argv[1]), sys.argv[2]


def log(entry: dict) -> None:
    with open(LOG, "a") as fh:
        fh.write(json.dumps(entry) + "\n")


def vector(text: str) -> list[float]:
    h = hashlib.sha256(text.encode()).digest()
    return [b / 255 for b in h[:32]] * 24  # 768 dims


class Handler(BaseHTTPRequestHandler):
    def log_message(self, *a):
        pass

    def reply(self, obj):
        body = json.dumps(obj).encode()
        self.send_response(200)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self):
        self.reply({"models": [{"name": "qwen3:30b"}, {"name": "qwen3:1.7b"}]})

    def do_POST(self):
        req = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
        if self.path == "/api/embed":
            chars = sum(len(t) for t in req["input"])
            log(
                {
                    "kind": "embed",
                    "model": req["model"],
                    "inputs": len(req["input"]),
                    "chars": chars,
                }
            )
            self.reply(
                {"embeddings": [vector(t) for t in req["input"]], "prompt_eval_count": chars // 4}
            )
            return
        prompt = "\n".join(str(m.get("content", "")) for m in req["messages"])
        is_summary = "relevance_score" in prompt
        if is_summary:
            text = json.dumps(
                {
                    "summary": "The excerpt reports a measurable effect relevant to the question.",
                    "relevance_score": 8,
                }
            )
        else:
            text = "The papers report a consistent effect (example2024 pages 1-2)."
        log(
            {
                "kind": "summary" if is_summary else "answer",
                "model": req["model"],
                "prompt_chars": len(prompt),
                "output_chars": len(text),
                "num_ctx": req.get("options", {}).get("num_ctx"),
            }
        )
        self.reply(
            {
                "model": req["model"],
                "done": True,
                "done_reason": "stop",
                "prompt_eval_count": len(prompt) // 4,
                "eval_count": len(text) // 4,
                "message": {"role": "assistant", "content": text},
            }
        )


ThreadingHTTPServer(("127.0.0.1", PORT), Handler).serve_forever()
