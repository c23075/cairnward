"""The Docling worker in the real sandbox (no network; reads the PDFs and its engine
folder only), with the stand-in `docling` package from tests/test_parsing.py."""

import subprocess
import sys
from pathlib import Path

import pytest

from cairnward.config import load_config
from cairnward.config.paths import Paths
from cairnward.sandbox import locate
from tests.test_parsing import FAKE_DOCLING

pytestmark = pytest.mark.skipif(not sys.platform.startswith("linux"), reason="Linux only")

NETWORK_PROBE = """
import socket
def blocked():
    try:
        socket.create_connection(("1.1.1.1", 443), timeout=3)
        return False
    except OSError:
        return True
assert blocked(), "the Docling worker could reach the network"
"""


def test_docling_worker_in_the_sandbox(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    from cairnward.research.engines import docling

    paths = Paths(tmp_path / "cfg", tmp_path / "data", tmp_path / "state")
    if locate(paths) is None:
        pytest.skip("sandbox-runtime not installed")
    monkeypatch.setenv("CAIRNWARD_CACHE_DIR", str(tmp_path / "cache"))
    root = docling.engine_dir(paths)
    subprocess.run([sys.executable, "-m", "venv", "--without-pip", str(root / "venv")], check=True)
    site = next((root / "venv" / "lib").glob("python*/site-packages"))
    for rel, code in FAKE_DOCLING.items():
        (site / rel).parent.mkdir(parents=True, exist_ok=True)
        text = code + (NETWORK_PROBE if rel.endswith("document_converter.py") else "")
        (site / rel).write_text(text)
    (root / "models").mkdir()
    papers = tmp_path / "papers"
    papers.mkdir()
    (papers / "good.pdf").write_bytes(b"%PDF")
    out = docling.run_worker(load_config(paths), "litreview-offline", [papers / "good.pdf"],
                             sandbox=True)  # fmt: skip
    assert out[0]["pages"][0] == "page 1 of good.pdf | a | table |", out
