"""`cairnward check` end to end against a real `serve` (isolation off) with a scripted
model: the report is written with every section, and holds no prompts or answers."""

import json
import sys
import threading
from http.server import ThreadingHTTPServer
from pathlib import Path

import pytest

from tests.isolation.harness import Cairnward

pytestmark = pytest.mark.skipif(sys.platform == "win32", reason="serve uses Unix sockets")


def test_check_writes_one_report(tmp_path: Path) -> None:
    from tests.isolation.test_paperqa_engine import ScriptedOllama

    srv = ThreadingHTTPServer(("127.0.0.1", 0), ScriptedOllama)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    cw = Cairnward(tmp_path, "off", backend_url=f"http://127.0.0.1:{srv.server_address[1]}",
                   models=("qwen3:30b", "qwen3:1.7b", "qwen3-embedding:0.6b"))  # fmt: skip
    report = tmp_path / "report.md"
    try:
        with cw:
            res = cw.cw("check", "--no-paperqa", "--out", str(report), timeout=900)
    finally:
        srv.shutdown()
    assert report.exists(), res.stdout + res.stderr
    text = report.read_text()
    for title in ("## Doctor", "## Server", "## Models", "## lit.ask eval (lean",
                  "## Coding eval", "## Chat clients"):  # fmt: skip
        assert title in text, text
    assert "| lean | " in text and "M1 exit criteria" in text
    assert "`qwen3:30b`" in text
    data = json.loads(report.with_suffix(".json").read_text())
    by = {s["title"].split(" (")[0]: s for s in data["sections"]}
    assert by["Server"]["status"] == "pass"
    assert len(by["lit.ask eval"]["data"]["results"]) == 8
    # the scripted model doesn't answer like a real one, so the eval itself fails here
    assert res.returncode == 1 and "report:" in res.stdout
    assert "working memory" not in text  # no answers in the report
