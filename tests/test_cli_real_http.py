"""CLI research commands against a model server over real HTTP (not a mock transport):
the HTTP clients are closed in the event loop that opened their connections, so a
successful index doesn't end in "Event loop is closed" (found on the reference machine)."""

import json
import threading
from collections.abc import Iterator
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any

import httpx
import pytest

from cairnward.cli import main
from tests.fakes.backends import FakeOllama
from tests.fakes.pdf import make_pdf


@pytest.fixture
def ollama() -> Iterator[int]:
    fake = FakeOllama()

    class Handler(BaseHTTPRequestHandler):
        protocol_version = "HTTP/1.1"  # keep-alive: the client holds a real connection

        def log_message(self, *a: Any) -> None:
            pass

        def _answer(self, method: str) -> None:
            body = self.rfile.read(int(self.headers.get("Content-Length") or 0))
            resp = fake.handler(httpx.Request(method, f"http://x{self.path}", content=body))
            data = resp.content
            self.send_response(resp.status_code)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(data)))
            self.end_headers()
            self.wfile.write(data)

        def do_POST(self) -> None:
            self._answer("POST")

        def do_GET(self) -> None:
            self._answer("GET")

    srv = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    yield srv.server_address[1]
    srv.shutdown()


def test_index_and_search_over_real_http(tmp_path: Path, ollama: int,
                                         monkeypatch: pytest.MonkeyPatch,
                                         capsys: pytest.CaptureFixture[str]) -> None:  # fmt: skip
    cfg = tmp_path / "cfg"
    cfg.mkdir()
    monkeypatch.setenv("CAIRNWARD_DATA_DIR", str(tmp_path / "data"))
    monkeypatch.setenv("CAIRNWARD_STATE_DIR", str(tmp_path / "state"))
    (cfg / "cairnward.toml").write_text('[privacy]\npreset = "strict"\nisolation = "off"\n')
    (cfg / "backends.toml").write_text(
        f'[backends.local-ollama]\ndriver = "ollama"\nurl = "http://127.0.0.1:{ollama}"\n'
        '[roles.main]\nbackend = "local-ollama"\nmodel = "m"\n'
        '[roles.fast]\nbackend = "local-ollama"\nmodel = "m"\n'
        '[roles.embed]\nbackend = "local-ollama"\nmodel = "e"\n'
    )
    papers = tmp_path / "papers"
    papers.mkdir()
    (papers / "a.pdf").write_bytes(make_pdf([["Sleep improved working memory."] * 20],
                                            title="Sleep"))  # fmt: skip
    base = ["--config-dir", str(cfg), "library"]
    assert main([*base, "create", "t", str(papers), "--env", "litreview-offline"]) == 0
    assert main([*base, "index", "t"]) == 0
    assert "1 added" in capsys.readouterr().out
    assert main([*base, "search", "t", "working memory", "--json"]) == 0
    assert json.loads(capsys.readouterr().out)
