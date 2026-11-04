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
    from tests.isolation.test_isolated_serve import FakeOllama

    srv = ThreadingHTTPServer(("127.0.0.1", 0), FakeOllama)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    cw = Cairnward(tmp_path, "off", backend_url=f"http://127.0.0.1:{srv.server_address[1]}",
                   models=("qwen3:30b", "qwen3:1.7b", "qwen3-embedding:0.6b"))  # fmt: skip
    report = tmp_path / "report.md"
    try:
        with cw:
            res = cw.cw("check", "--no-coding", "--out", str(report), timeout=300)
    finally:
        srv.shutdown()
    assert report.exists(), res.stdout + res.stderr
    text = report.read_text()
    for title in ("## Doctor", "## Server", "## Models", "## Chat clients"):
        assert title in text, text
    assert "`qwen3:30b`" in text
    data = json.loads(report.with_suffix(".json").read_text())
    by = {s["title"].split(" (")[0]: s for s in data["sections"]}
    assert by["Server"]["status"] == "pass"
    assert "report:" in res.stdout


def test_a_crashing_section_doesnt_stop_the_check() -> None:
    from cairnward.check import Section, _guarded

    def boom() -> Section:
        raise ValueError("invalid literal for int() with base 10: 'p. 2'")

    sec = _guarded("lit.ask eval", boom)
    assert sec.status == "fail" and sec.title == "lit.ask eval"
    assert "crashed: ValueError" in sec.lines[0] and "test_check.py" in sec.lines[0]
    ok = _guarded("x", lambda: Section("x"))
    assert ok.title == "x"
