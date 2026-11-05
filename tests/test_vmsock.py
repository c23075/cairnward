"""The seccomp filter that refuses VM sockets in the isolated core (I1, T-28). It runs for real
in a child process: no stand-in for the kernel."""

from __future__ import annotations

import json
import platform
import struct
import subprocess
import sys

import pytest

from cairnward.sandbox import vmsock

linux = pytest.mark.skipif(
    platform.system() != "Linux" or vmsock.machine() not in vmsock.ARCHES,
    reason="seccomp filter: Linux on x86-64 or arm64",
)

PROBE = r"""
import ctypes, errno, json, socket, subprocess, sys
from cairnward.sandbox import vmsock

def vsock():
    try:
        socket.socket(40, socket.SOCK_STREAM).close()
        return "opened"
    except OSError as e:
        return errno.errorcode[e.errno]

before = vsock()
vmsock.apply()
libc = ctypes.CDLL(None, use_errno=True)
uring = libc.syscall(425, 4, ctypes.c_void_p(0))
uring_errno = errno.errorcode.get(ctypes.get_errno())
socket.socket(socket.AF_UNIX).close()
socket.socket(socket.AF_INET).close()
child = subprocess.run([sys.executable, "-c", "import socket; socket.socket(40)"],
                       capture_output=True, text=True)
print(json.dumps({"before": before, "after": vsock(), "uring": [uring, uring_errno],
                  "child_refused": "Errno 1]" in child.stderr,
                  "refused": vmsock.vm_sockets_refused()}))
"""


@linux
def test_filter_refuses_vm_sockets_and_io_uring_only() -> None:
    out = subprocess.run([sys.executable, "-c", PROBE], capture_output=True, text=True,
                         timeout=60, check=True)  # fmt: skip
    got = json.loads(out.stdout)
    # EPERM is the filter's answer; a kernel without VM sockets would say EAFNOSUPPORT
    assert got["after"] == "EPERM", got
    assert got["uring"] == [-1, "EPERM"]
    assert got["child_refused"] and got["refused"]  # inherited by everything the core starts


@pytest.mark.parametrize("arch", sorted(vmsock.ARCHES))
def test_program_shape(arch: str) -> None:
    code = vmsock.program(arch)
    assert len(code) % 8 == 0 and len(code) // 8 <= 12
    ins = [struct.unpack("=HBBI", code[i : i + 8]) for i in range(0, len(code), 8)]
    assert ins[-1] == (vmsock.BPF_RET_K, 0, 0, vmsock.RET_ERRNO | vmsock.EPERM)
    assert ins[-2] == (vmsock.BPF_RET_K, 0, 0, vmsock.RET_ALLOW)
    for n, (code_, jt, jf, _) in enumerate(ins):  # every jump lands inside the program
        if code_ in (vmsock.BPF_JEQ_K, vmsock.BPF_JGE_K):
            assert n + 1 + max(jt, jf) < len(ins)
    audit = vmsock.ARCHES[arch][0]
    assert ins[1][0] == vmsock.BPF_JEQ_K and ins[1][3] == audit  # a foreign ABI is refused


def test_unknown_architecture_is_refused() -> None:
    with pytest.raises(vmsock.Unsupported):
        vmsock.program("riscv64")


def test_core_wont_start_under_wsl_without_the_filter(monkeypatch: pytest.MonkeyPatch) -> None:
    from cairnward.core import server

    def fail() -> None:
        raise vmsock.Unsupported("no seccomp here")

    monkeypatch.setattr(vmsock, "apply", fail)
    monkeypatch.setattr("cairnward.sysinfo.wsl_version", lambda: 2)
    with pytest.raises(SystemExit, match="won't start"):
        server.refuse_vm_sockets(lambda _: None)
    monkeypatch.setattr("cairnward.sysinfo.wsl_version", lambda: None)
    said: list[str] = []
    server.refuse_vm_sockets(said.append)  # elsewhere: reported, not fatal
    assert "aren't refused" in said[0]
