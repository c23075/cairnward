"""The WSL rules in the real sandbox, on plain Linux: folders stand in for the Windows drive and
/init. What only real WSL can show (interop itself, 9p mounts) is in docs/wsl2.md."""

import os
import subprocess
import sys
from pathlib import Path

import pytest

from cairnward import doctor
from cairnward.config.paths import Paths
from cairnward.sandbox import SandboxSpec, locate, srt, wrap

pytestmark = pytest.mark.skipif(not sys.platform.startswith("linux"), reason="Linux only")


@pytest.fixture
def box(tmp_path: Path) -> list[str]:
    cmd = locate(
        Paths(config_dir=tmp_path / "c", data_dir=tmp_path / "d", state_dir=tmp_path / "s")
    )
    if cmd is None:
        pytest.skip("sandbox-runtime isn't installed")
    return cmd


@pytest.fixture
def windows(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> tuple[Path, Path]:
    drive = tmp_path / "drive"
    (drive / "Users" / "me").mkdir(parents=True)
    (drive / "Users" / "me" / "secret.txt").write_text("private")
    init = tmp_path / "init"
    init.write_text("#!/bin/sh\necho reached\n")
    init.chmod(0o755)
    monkeypatch.setattr(srt, "windows_paths", lambda: [drive, init])
    monkeypatch.setattr(doctor, "windows_paths", lambda: [drive, init])
    return drive, init


def run_in(
    box: list[str], tmp_path: Path, ws: Path, *command: str
) -> subprocess.CompletedProcess[str]:
    spec = SandboxSpec(allowed_hosts=[], allow_write=[ws], deny_read=[])
    env = {k: os.environ[k] for k in ("PATH", "HOME") if k in os.environ}
    return subprocess.run(wrap(box, spec.write(tmp_path / "srt.json"), list(command)),
                          capture_output=True, text=True, timeout=60, cwd=ws, check=False,
                          env=env)  # fmt: skip


def test_self_test_covers_windows(box: list[str], windows: tuple[Path, Path]) -> None:
    check = doctor.self_test(box)
    assert check.status == "ok", check
    assert "Windows drives" in check.detail


def test_self_test_notices_a_drive_left_open(
    box: list[str], windows: tuple[Path, Path], monkeypatch: pytest.MonkeyPatch
) -> None:
    drive, _ = windows
    monkeypatch.setattr(srt, "windows_paths", list)  # the sandbox forgets, the probe doesn't
    check = doctor.self_test(box)
    assert check.status == "fail" and "windows_paths" in check.detail, check
    assert drive.exists()


def test_drive_and_init_are_hidden(
    box: list[str], tmp_path: Path, windows: tuple[Path, Path]
) -> None:
    drive, init = windows
    ws = tmp_path / "ws"
    ws.mkdir()
    res = run_in(box, tmp_path, ws, "sh", "-c", f"ls -A {drive}; cat {drive}/Users/me/secret.txt; "
                                                f"{init}")  # fmt: skip
    assert "private" not in res.stdout and "Users" not in res.stdout
    assert "reached" not in res.stdout  # what the kernel would hand a Windows program to


def test_a_workspace_on_the_drive_still_works(
    box: list[str], tmp_path: Path, windows: tuple[Path, Path]
) -> None:
    drive, _ = windows
    ws = drive / "Users" / "me" / "project"
    ws.mkdir()
    (ws / "in.txt").write_text("hello")
    res = run_in(box, tmp_path, ws, "sh", "-c",
                 f"cat in.txt; echo done > out.txt; cat {drive}/Users/me/secret.txt")  # fmt: skip
    assert "hello" in res.stdout and "private" not in res.stdout
    assert (ws / "out.txt").read_text().strip() == "done"
