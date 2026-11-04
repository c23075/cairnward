"""Startup checks shared by every server mode."""

import os
import subprocess
import sys
from pathlib import Path

import pytest

from cairnward.config import load_config
from cairnward.config.paths import Paths
from cairnward.core.server import serve, socket_path_problem

pytestmark = pytest.mark.skipif(os.name == "nt", reason="Unix sockets")


def test_too_long_state_path_stops_serve_with_a_clear_message(tmp_path: Path) -> None:
    deep = tmp_path / ("x" * 120)
    cfg = load_config(Paths(tmp_path / "cfg", tmp_path / "data", deep))
    problem = socket_path_problem(cfg)
    assert problem and "CAIRNWARD_STATE_DIR" in problem
    said: list[str] = []
    assert serve(cfg, log=said.append) == 2
    assert said and "too long for Unix sockets" in said[0]


def test_ordinary_state_path_is_fine(tmp_path: Path) -> None:
    cfg = load_config(Paths(tmp_path / "cfg", tmp_path / "data", Path("/run/user/1000/cw")))
    assert socket_path_problem(cfg) is None


def test_outer_process_doesnt_import_what_only_the_core_needs() -> None:
    """With isolation on, Python starts twice, so these would be paid for twice (NFR-P5)."""
    code = (
        "import sys, cairnward.core.server; "
        "print([m for m in ('mcp', 'cairnward.gateway.app', 'cairnward.mcp.hub', "
        "'cairnward.dashboard') if m in sys.modules])"
    )
    out = subprocess.run([sys.executable, "-c", code], capture_output=True, text=True, check=True)
    assert out.stdout.strip() == "[]"
