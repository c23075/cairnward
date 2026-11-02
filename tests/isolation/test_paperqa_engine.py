"""PaperQA2 as the optional deep engine (FR-RES-13), end to end: an isolated `serve`, a
library indexed through the gateway, and `library ask --engine paperqa` running the
worker in the sandbox with only the gateway reachable. A scripted model stands in for
Ollama. Skipped unless CAIRNWARD_PAPERQA_VENV points at an environment with paper-qa
(`cairnward research engine install paperqa` makes one) and srt is installed."""

import hashlib
import json
import os
import re
import subprocess
import sys
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any

import pytest

from cairnward.config.paths import Paths
from cairnward.sandbox import locate
from tests.fakes.pdf import make_pdf
from tests.isolation.test_isolated_serve import free_port

VENV = os.environ.get("CAIRNWARD_PAPERQA_VENV")
pytestmark = pytest.mark.skipif(not VENV or not sys.platform.startswith("linux"),
                                reason="needs CAIRNWARD_PAPERQA_VENV (Linux)")  # fmt: skip
CALLS: list[dict[str, Any]] = []


class ScriptedOllama(BaseHTTPRequestHandler):
    def log_message(self, *a: Any) -> None:
        pass

    def send(self, obj: dict[str, Any]) -> None:
        body = json.dumps(obj).encode()
        self.send_response(200)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self) -> None:
        self.send({"models": []})

    def do_POST(self) -> None:
        req = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
        if self.path == "/api/embed":
            CALLS.append({"kind": "embed", "model": req["model"]})
            vecs = [[b / 255 for b in hashlib.sha256(t.encode()).digest()] * 8
                    for t in req["input"]]  # fmt: skip
            self.send({"embeddings": vecs, "prompt_eval_count": 10})
            return
        prompt = "\n".join(str(m.get("content", "")) for m in req["messages"])
        CALLS.append({"kind": "chat", "model": req["model"]})
        if "relevance_score" in prompt:
            text = json.dumps({"summary": "Longer sleep went with better working memory.",
                               "relevance_score": 8})  # fmt: skip
        else:
            key = re.search(r"\b(pqac-[0-9a-f]{8})", prompt)  # PaperQA2's context ids
            text = f"Longer sleep improved working memory ({key.group(1) if key else 'none'})."
        self.send({"model": req["model"], "done": True, "done_reason": "stop",
                   "prompt_eval_count": len(prompt) // 4, "eval_count": 20,
                   "message": {"role": "assistant", "content": text}})  # fmt: skip


def test_paperqa_through_cairnward(tmp_path: Path) -> None:
    paths = Paths(tmp_path / "cfg", tmp_path / "data", tmp_path / "state")
    env = {**os.environ, "CAIRNWARD_CONFIG_DIR": str(paths.config_dir),
           "CAIRNWARD_DATA_DIR": str(paths.data_dir), "CAIRNWARD_STATE_DIR": str(paths.state_dir),
           "CAIRNWARD_CACHE_DIR": str(tmp_path / "cache")}  # fmt: skip
    if locate(paths) is None:
        pytest.skip("sandbox-runtime not installed")
    gw, dash, eg = free_port(), free_port(), free_port()
    srv = ThreadingHTTPServer(("127.0.0.1", 0), ScriptedOllama)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    (paths.config_dir / "environments").mkdir(parents=True)
    (paths.config_dir / "cairnward.toml").write_text(
        f"[server]\ngateway_port = {gw}\ndashboard_port = {dash}\negress_port = {eg}\n"
        '[privacy]\npreset = "strict"\n'
    )
    (paths.config_dir / "backends.toml").write_text(
        f'[backends.local-ollama]\ndriver = "ollama"\nurl = "http://127.0.0.1:{srv.server_address[1]}"\n'
        '[roles.main]\nbackend = "local-ollama"\nmodel = "qwen3:30b"\n'
        '[roles.fast]\nbackend = "local-ollama"\nmodel = "qwen3:1.7b"\n'
        '[roles.embed]\nbackend = "local-ollama"\nmodel = "qwen3-embedding:0.6b"\n'
    )
    engine = paths.data_dir / "engines" / "paperqa"
    engine.mkdir(parents=True)
    (engine / "venv").symlink_to(VENV or "")
    papers = tmp_path / "papers"
    papers.mkdir()
    page = [
        f"Line {i}: participants who slept longer had better working memory." for i in range(30)
    ]
    (papers / "sleep.pdf").write_bytes(make_pdf([page, page], title="Sleep and Memory"))
    cw = [sys.executable, "-m", "cairnward"]
    serve = subprocess.Popen(
        [*cw, "serve"],
        env=env,
        stdin=subprocess.DEVNULL,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        text=True,
    )
    try:
        st = subprocess.run([*cw, "status", "--wait", "60"], env=env, capture_output=True,
                            text=True, check=False)  # fmt: skip
        assert "core isolated" in st.stdout, st.stdout + st.stderr
        for step in (["library", "create", "thesis", str(papers), "--env", "litreview-offline"],
                     ["library", "index", "thesis"]):  # fmt: skip
            r = subprocess.run([*cw, *step], env=env, capture_output=True, text=True, check=False)
            assert r.returncode == 0, r.stdout + r.stderr
        ask = subprocess.run(
            [*cw, "library", "ask", "thesis", "Does sleep help working memory?", "--engine",
             "paperqa", "--json", "--evidence-k", "3"],
            env=env, capture_output=True, text=True, timeout=900, check=False,
        )  # fmt: skip
        assert ask.returncode == 0, ask.stdout + ask.stderr
        answer = json.loads(ask.stdout)
    finally:
        serve.terminate()
        try:
            serve.wait(20)
        except subprocess.TimeoutExpired:
            serve.kill()
        srv.shutdown()
    assert answer["sources"] and answer["sources"][0]["file"] == "sleep.pdf"
    assert answer["text"].startswith("Longer sleep improved working memory [1]")
    assert not answer["uncited"]
    # every call reached the model server through the gateway, with Cairnward's models
    assert {c["model"] for c in CALLS} <= {"qwen3:30b", "qwen3:1.7b", "qwen3-embedding:0.6b"}
    audit = subprocess.run([*cw, "audit", "log", "--json", "--last", "200"], env=env,
                           capture_output=True, text=True, check=True).stdout  # fmt: skip
    recs = [json.loads(x) for x in audit.splitlines()]
    calls = [r for r in recs if r["action"] == "model.call" and r["client"] == "paperqa"
             and r["phase"] == "pre"]  # fmt: skip
    assert calls, "PaperQA2's calls should appear in the audit log as client 'paperqa'"
    assert any(r["action"] == "research.ask" and r["details"].get("engine") == "paperqa"
               for r in recs)  # fmt: skip
