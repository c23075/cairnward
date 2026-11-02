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

from tests.fakes.pdf import make_pdf
from tests.isolation.harness import MODES, Cairnward

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


@pytest.mark.parametrize("mode", MODES)
def test_paperqa_through_cairnward(tmp_path: Path, mode: str) -> None:
    """isolated: the core is sandboxed, so the outer process runs the worker for jobs;
    hybrid/off: the library's environment has isolation off, so the server runs it
    (unsandboxed, as the setting says)."""
    CALLS.clear()
    srv = ThreadingHTTPServer(("127.0.0.1", 0), ScriptedOllama)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    harness = Cairnward(tmp_path, mode, backend_url=f"http://127.0.0.1:{srv.server_address[1]}",
                        models=("qwen3:30b", "qwen3:1.7b", "qwen3-embedding:0.6b"))  # fmt: skip
    paths, env = harness.paths, harness.env
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
    harness.__enter__()
    try:
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
        # the same through an overnight-style job, run by the server
        questions = tmp_path / "questions.txt"
        questions.write_text("Does sleep help working memory?\n")
        queued = subprocess.run([*cw, "research", "ask", "thesis", str(questions), "--engine",
                                 "paperqa", "--evidence-k", "3"], env=env, capture_output=True,
                                text=True, check=False)  # fmt: skip
        assert queued.returncode == 0, queued.stdout + queued.stderr
        job_id = queued.stdout.split()[1]
        shown = harness.wait_job(job_id)
        assert " done " in shown and "[ok ]" in shown, shown
        result = subprocess.run([*cw, "jobs", "result", job_id], env=env, capture_output=True,
                                text=True, check=True).stdout  # fmt: skip
        assert "Longer sleep improved working memory [1]" in result and "sleep.pdf" in result
        # a review of chosen files, one of them added after queuing: indexed by the core
        new = papers / "later" / "nap.pdf"
        queued = subprocess.run([*cw, "research", "review", "thesis", "--question", "Sleep?",
                                 "--files", str(new), "--at", "00:00"], env=env,
                                capture_output=True, text=True, check=False)  # fmt: skip
        assert queued.returncode == 0 and "later/nap.pdf" in queued.stdout, queued.stderr
        review_id = queued.stdout.split()[1]
        new.parent.mkdir()
        new.write_bytes(make_pdf([page], title="Naps and Memory"))
        # "--at 00:00" is the next midnight; start it now by editing the saved job
        job_file = paths.data_dir / "jobs" / review_id / "job.json"
        saved = json.loads(job_file.read_text())
        job_file.write_text(json.dumps(saved | {"not_before": None}))
        shown = harness.wait_job(review_id)
        assert " done " in shown and "[ok ] prepare" in shown, shown
        result = subprocess.run([*cw, "jobs", "result", review_id], env=env,
                                capture_output=True, text=True, check=True).stdout  # fmt: skip
        assert "| Naps and Memory |" in result and "Sleep and Memory" not in result, result
    finally:
        harness.__exit__()
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
    asks = [r for r in recs if r["action"] == "research.ask"
            and r["details"].get("engine") == "paperqa"]  # fmt: skip
    assert {r["actor"] for r in asks} == {"user", f"job:{job_id}"}
    job_run = next(r["run_id"] for r in asks if r["actor"] == f"job:{job_id}")
    assert any(r["action"] == "model.call" and r["run_id"] == job_run for r in recs)
    assert any(r["action"] == "run.end" and r["run_id"] == job_run and r["outcome"] == "ok"
               for r in recs)  # fmt: skip
