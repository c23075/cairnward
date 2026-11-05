"""I1 in the real sandbox. The core's sandbox allows Unix sockets, so srt skips the filter that
also refuses VM sockets. Under WSL2 those reach Windows. The core's own filter closes that."""

from __future__ import annotations

import subprocess
import sys
from pathlib import Path

import pytest

from cairnward.config.paths import Paths
from cairnward.sandbox import SandboxSpec, locate, wrap

pytestmark = pytest.mark.skipif(not sys.platform.startswith("linux"), reason="Linux only")

PROBE = """
import errno, socket, sys
if sys.argv[1:] == ["filter"]:
    from cairnward.sandbox import vmsock
    vmsock.apply()
def outcome(family):
    try:
        socket.socket(family, socket.SOCK_STREAM).close()
        return "opened"
    except OSError as e:
        return errno.errorcode[e.errno]
print(outcome(40), outcome(socket.AF_UNIX))
"""


@pytest.fixture
def box(tmp_path: Path) -> list[str]:
    cmd = locate(Paths(tmp_path / "c", tmp_path / "d", tmp_path / "s"))
    if cmd is None:
        pytest.skip("sandbox-runtime isn't installed")
    return cmd


def run(box: list[str], tmp_path: Path, *, core_like: bool, filtered: bool) -> list[str]:
    (tmp_path / "probe.py").write_text(PROBE)
    spec = SandboxSpec(allowed_hosts=[], allow_write=[tmp_path], deny_read=[],
                       allow_all_unix_sockets=core_like)  # fmt: skip
    argv = [sys.executable, str(tmp_path / "probe.py"), *(["filter"] if filtered else [])]
    cmd = wrap(box, spec.write(tmp_path / "srt.json"), argv)
    res = subprocess.run(cmd, capture_output=True, text=True, timeout=60, cwd=tmp_path, check=False)
    assert res.returncode == 0, res.stderr
    return res.stdout.split()


def test_core_sandbox_refuses_vm_sockets_but_keeps_unix_sockets(
    box: list[str], tmp_path: Path
) -> None:
    vsock, unix = run(box, tmp_path, core_like=True, filtered=True)
    assert (vsock, unix) == ("EPERM", "opened")


def test_without_the_filter_the_core_sandbox_lets_vm_sockets_through(
    box: list[str], tmp_path: Path
) -> None:
    """What I1 was. Where the kernel has no VM sockets at all, there's nothing to show."""
    vsock, unix = run(box, tmp_path, core_like=True, filtered=False)
    if vsock == "EAFNOSUPPORT":
        pytest.skip("this kernel has no VM sockets")
    assert (vsock, unix) == ("opened", "opened")


def test_doctor_notices_when_the_core_would_get_vm_sockets(
    box: list[str], tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The self-test used to probe only ordinary sandboxes, where srt's filter already refuses
    VM sockets, so it couldn't see I1. It now probes one set up like the core's."""
    from cairnward import doctor

    assert doctor.self_test(box).status == "ok"
    no_filter = (
        "import socket\ntry:\n    socket.socket(40).close()\n    print('open')\n"
        "except OSError:\n    print('refused')\n"
    )
    monkeypatch.setattr(doctor, "CORE_PROBE", no_filter)
    check = doctor.self_test(box)
    if check.status == "ok":
        pytest.skip("this kernel has no VM sockets, so nothing leaks without the filter")
    assert "core_vm_socket" in check.detail
