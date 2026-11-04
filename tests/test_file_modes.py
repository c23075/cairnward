"""Cairnward's files are owner-only: real CLI, real SQLite."""

import os
import stat
import subprocess
import sys
from pathlib import Path

import pytest

from cairnward.config.paths import Paths, make_owner_only, wider_than_owner
from cairnward.doctor import check_file_modes

pytestmark = pytest.mark.skipif(os.name == "nt", reason="POSIX file modes")


def paths_in(tmp_path: Path) -> Paths:
    return Paths(tmp_path / "cfg", tmp_path / "data", tmp_path / "state")


def test_a_real_cli_run_creates_only_owner_files(tmp_path: Path) -> None:
    """Whatever umask it was started with, the databases, including SQLite's own, stay private."""
    paths = paths_in(tmp_path)
    env = {
        **os.environ,
        "CAIRNWARD_DATA_DIR": str(paths.data_dir),
        "CAIRNWARD_STATE_DIR": str(paths.state_dir),
        "CAIRNWARD_CONFIG_DIR": str(paths.config_dir),
    }
    code = ("import os; os.umask(0o022); from cairnward.cli import main; "
            "raise SystemExit(main(['store', 'info']))")  # fmt: skip
    res = subprocess.run([sys.executable, "-c", code], env=env, capture_output=True, text=True,
                         check=False)  # fmt: skip
    assert res.returncode == 0, res.stderr
    assert (paths.data_dir / "keys.db").exists()
    assert wider_than_owner(paths) == []


def test_older_files_are_tightened_and_stay_usable(tmp_path: Path) -> None:
    paths = paths_in(tmp_path)
    tool = paths.data_dir / "engines" / "bin" / "tool"
    tool.parent.mkdir(parents=True)
    tool.write_text("#!/bin/sh\n")
    tool.chmod(0o775)
    keys = paths.data_dir / "keys.db"
    keys.write_text("x")
    keys.chmod(0o644)
    paths.data_dir.chmod(0o775)
    assert set(wider_than_owner(paths)) >= {tool, keys, paths.data_dir}
    assert make_owner_only(paths) >= 3
    assert stat.S_IMODE(keys.stat().st_mode) == 0o600
    assert stat.S_IMODE(tool.stat().st_mode) == 0o700  # still runs
    assert wider_than_owner(paths) == []


def test_sockets_and_missing_folders_are_fine(tmp_path: Path) -> None:
    assert wider_than_owner(paths_in(tmp_path)) == []


def test_doctor_reports_wide_files(tmp_path: Path) -> None:
    from types import SimpleNamespace

    paths = paths_in(tmp_path)
    paths.data_dir.mkdir()
    paths.data_dir.chmod(0o700)
    secret = paths.data_dir / "audit.jsonl"
    secret.write_text("x")
    secret.chmod(0o664)
    check = check_file_modes(SimpleNamespace(paths=paths))
    assert check.status == "warn" and "1 readable" in check.detail
    make_owner_only(paths)
    assert check_file_modes(SimpleNamespace(paths=paths)).status == "ok"
