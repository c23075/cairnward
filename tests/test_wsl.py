"""WSL handling, against a stand-in /proc in WSL's format. This never runs on real WSL here:
docs/wsl2.md lists what has to be checked on a real machine."""

import shutil
import subprocess
import sys
from pathlib import Path
from typing import Any

import pytest

from cairnward import doctor, sysinfo
from cairnward.config.loader import load_config
from cairnward.config.paths import Paths
from cairnward.core import server
from cairnward.run import PROFILES, plan_run
from cairnward.run import runner as run_runner
from cairnward.sandbox import SandboxSpec, SandboxUnavailable, require, srt
from cairnward.sysinfo import on_windows_drive, windows_mounts, wsl_interop_on, wsl_version

pytestmark = pytest.mark.skipif(not sys.platform.startswith("linux"), reason="Linux only")

WSL2_RELEASE = "6.6.87.2-microsoft-standard-WSL2\n"
WSL1_RELEASE = "4.4.0-19041-Microsoft\n"
MOUNTS = """\
none /usr/lib/modules/6.6.87.2-microsoft-standard-WSL2 overlay rw,relatime 0 0
/dev/sdc / ext4 rw,relatime,discard,errors=remount-ro 0 0
none /mnt/wsl tmpfs rw,relatime 0 0
drivers /usr/lib/wsl/drivers 9p ro,nosuid,nodev,noatime,aname=drivers;fmask=222;dmask=222 0 0
C:\\134 /mnt/c 9p rw,noatime,dirsync,aname=drvfs;path=C:\\;uid=1000;gid=1000 0 0
D:\\134 /mnt/my\\040disk 9p rw,noatime,dirsync,aname=drvfs;path=D:\\;uid=1000;gid=1000 0 0
"""


def fake_proc(tmp_path: Path, release: str, interop: str | None = None) -> Path:
    proc = tmp_path / "proc"
    (proc / "sys/kernel").mkdir(parents=True)
    (proc / "sys/kernel/osrelease").write_text(release)
    (proc / "mounts").write_text(MOUNTS)
    if interop is not None:
        (proc / "sys/fs/binfmt_misc").mkdir(parents=True)
        (proc / "sys/fs/binfmt_misc/WSLInterop").write_text(interop)
    return proc


@pytest.fixture
def paths(tmp_path: Path) -> Paths:
    return Paths(config_dir=tmp_path / "cfg", data_dir=tmp_path / "data", state_dir=tmp_path / "st")


@pytest.mark.parametrize(
    ("release", "interop", "expected"),
    [
        (WSL2_RELEASE, None, 2),
        (WSL1_RELEASE, None, 1),
        ("6.6.0-my-own-kernel\n", "enabled\n", 2),  # a custom WSL2 kernel
        ("6.12.113+deb13-amd64\n", None, None),
    ],
)
def test_wsl_version(tmp_path: Path, release: str, interop: str | None, expected: int) -> None:
    assert wsl_version(fake_proc(tmp_path, release, interop)) == expected


def test_no_proc_is_not_wsl(tmp_path: Path) -> None:
    assert wsl_version(tmp_path / "missing") is None
    assert windows_mounts(tmp_path / "missing") == []


def test_windows_mounts_follow_the_mount_table(tmp_path: Path) -> None:
    mounts = windows_mounts(fake_proc(tmp_path, WSL2_RELEASE))
    assert mounts == [Path("/usr/lib/wsl/drivers"), Path("/mnt/c"), Path("/mnt/my disk")]


def test_on_windows_drive(tmp_path: Path) -> None:
    proc = fake_proc(tmp_path, WSL2_RELEASE)
    assert on_windows_drive(Path("/mnt/c/Users/me"), proc)
    assert on_windows_drive(Path("/mnt/c"), proc)
    assert not on_windows_drive(Path("/mnt/cache"), proc)
    assert not on_windows_drive(Path("/home/me"), proc)
    # a link in the Linux file system doesn't change where the files are
    mount = tmp_path / "drive"
    mount.mkdir()
    (proc / "mounts").write_text(f"C:\\134 {mount} 9p rw 0 0\n")
    (tmp_path / "link").symlink_to(mount)
    assert on_windows_drive(tmp_path / "link" / "x", proc)
    # the same path off WSL
    assert not on_windows_drive(Path("/mnt/c/x"), fake_proc(tmp_path / "plain", "6.12.0\n"))


@pytest.mark.parametrize(("text", "expected"), [("enabled\ninterpreter /init\n", True),
                                                ("disabled\n", False)])  # fmt: skip
def test_interop(tmp_path: Path, text: str, expected: bool) -> None:
    assert wsl_interop_on(fake_proc(tmp_path, WSL2_RELEASE, text)) is expected


def test_interop_absent(tmp_path: Path) -> None:
    assert not wsl_interop_on(fake_proc(tmp_path, WSL2_RELEASE))


@pytest.fixture
def on_wsl(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """WSL2, with one real folder as its C: drive."""
    drive = tmp_path / "mnt-c"
    drive.mkdir()
    proc = fake_proc(tmp_path, WSL2_RELEASE)
    (proc / "mounts").write_text(f"C:\\134 {drive} 9p rw 0 0\n")
    monkeypatch.setattr(sysinfo, "PROC", proc)
    for module in (sysinfo, srt, doctor, run_runner):
        monkeypatch.setattr(module, "wsl_version", lambda p=proc: 2)
    monkeypatch.setattr(srt, "windows_mounts", lambda p=proc: [drive])
    monkeypatch.setattr(sysinfo, "windows_mounts", lambda p=proc: [drive])
    return drive


def test_every_sandbox_hides_windows(on_wsl: Path) -> None:
    deny = SandboxSpec(allowed_hosts=[], allow_write=[], deny_read=[]).settings()["filesystem"][
        "denyRead"
    ]
    assert str(on_wsl) in deny  # wherever a spec comes from


def test_paths_that_do_not_exist_stay_out(on_wsl: Path) -> None:
    deny = srt.windows_paths()
    assert Path("/run/WSL") in deny and Path("/init") in deny and on_wsl in deny


def test_elsewhere_nothing_changes(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    monkeypatch.setattr(srt, "wsl_version", lambda: None)
    assert srt.windows_paths() == []
    hidden = tmp_path / "hidden"
    hidden.mkdir()
    spec = SandboxSpec(allowed_hosts=[], allow_write=[], deny_read=[hidden])
    assert spec.settings()["filesystem"]["denyRead"] == [str(hidden)]


def test_wsl1_can_not_be_isolated(paths: Paths, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(srt, "wsl_version", lambda: 1)
    with pytest.raises(SandboxUnavailable, match="WSL2"):
        require(paths)


def test_doctor_reports_wsl(paths: Paths, monkeypatch: pytest.MonkeyPatch, on_wsl: Path) -> None:
    cfg = load_config(paths)
    monkeypatch.setattr(doctor, "wsl_interop_on", lambda: True)
    monkeypatch.setattr(doctor, "on_windows_drive", lambda p: False)
    got = {c.name: c.status for c in doctor.check_wsl(cfg)}
    assert got == {"WSL": "ok", "WSL interop": "warn"}
    monkeypatch.setattr(doctor, "wsl_interop_on", lambda: False)
    monkeypatch.setattr(doctor, "on_windows_drive", lambda p: True)
    got = {c.name: c.status for c in doctor.check_wsl(cfg)}
    assert got == {"WSL": "ok", "WSL folders": "fail"}
    monkeypatch.setattr(doctor, "wsl_version", lambda: 1)
    assert [c.status for c in doctor.check_wsl(cfg)] == ["fail"]


def test_doctor_is_silent_elsewhere(paths: Paths, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(doctor, "wsl_version", lambda: None)
    assert doctor.check_wsl(load_config(paths)) == []


def test_disk_encryption_points_at_windows(monkeypatch: pytest.MonkeyPatch, on_wsl: Path) -> None:
    monkeypatch.setattr(shutil, "which", lambda name: "/bin/lsblk")
    monkeypatch.setattr(sys, "platform", "linux")

    class Done:
        stdout = "disk\ndisk\n"

    monkeypatch.setattr(subprocess, "run", lambda *a, **k: Done())
    check = doctor.check_disk_encryption()
    assert check.status == "warn" and "BitLocker" in check.fix


def test_serve_refuses_folders_on_a_windows_drive(
    paths: Paths, monkeypatch: pytest.MonkeyPatch
) -> None:
    cfg = load_config(paths)
    logged: list[str] = []
    monkeypatch.setattr(server, "on_windows_drive", lambda p: True)
    assert server.serve(cfg, log=logged.append) == 2
    assert "Windows drive" in logged[0]


def plan_at(paths: Paths, tmp_path: Path, workspace: Path, privacy: str) -> Any:
    paths.config_dir.mkdir(parents=True, exist_ok=True)
    (paths.config_dir / "environments").mkdir(exist_ok=True)
    (paths.config_dir / "environments" / "e.toml").write_text(f"[privacy]\n{privacy}\n")
    run = {"run_id": "r1", "gateway_key": "cw-run-k", "egress_token": "tok",
           "gateway_port": 8484, "egress_port": 8486}  # fmt: skip
    return plan_run(load_config(paths), environment="e", profile=PROFILES["aider"],
                    model="cw/e:medium", workspace=workspace, command=["sh"], run=run,
                    srt=["srt"] if "strict" in privacy else None,
                    settings_file=tmp_path / "srt.json", tmp_dir=tmp_path)  # fmt: skip


def test_a_workspace_on_a_drive_says_so(paths: Paths, tmp_path: Path, on_wsl: Path,
                                        monkeypatch: pytest.MonkeyPatch) -> None:  # fmt: skip
    monkeypatch.setattr(run_runner, "on_windows_drive", lambda p: p.is_relative_to(on_wsl))
    ws = on_wsl / "project"
    ws.mkdir()
    plan = plan_at(paths, tmp_path, ws, 'preset = "strict"')
    assert any("Windows drive" in w for w in plan.warnings)
    assert ws in plan.spec.allow_write
    local = tmp_path / "ws"
    local.mkdir()
    plan = plan_at(paths, tmp_path, local, 'preset = "strict"')
    assert not any("Windows drive" in w for w in plan.warnings)


def test_no_isolation_under_wsl_warns_about_windows(paths: Paths, tmp_path: Path,
                                                    on_wsl: Path) -> None:  # fmt: skip
    ws = tmp_path / "ws"
    ws.mkdir()
    plan = plan_at(paths, tmp_path, ws, 'isolation = "off"')
    assert any("Windows files" in w for w in plan.warnings)
