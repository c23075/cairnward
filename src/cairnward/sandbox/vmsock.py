"""Refuse VM sockets in the isolated core (I1, T-28).

The core needs Unix sockets, so its srt sandbox sets `allowAllUnixSockets`. srt then skips its
seccomp helper, and that helper is also what refuses `socket(AF_VSOCK, ...)`: a network namespace
doesn't fence VM sockets, so under WSL2 the core could reach Windows past egress (srt 0.0.81
documents this on `allowAllUnixSockets`).

This installs a small seccomp filter on the core itself, inherited by everything it starts:
- `socket(AF_VSOCK, ...)` fails with EPERM;
- `io_uring_setup` fails with EPERM: io_uring can create a socket without calling `socket()`;
- a system call from a non-native ABI (32-bit `int 0x80`, or x32 on x86-64) fails with EPERM,
  since the filter's numbers are for the native ABI and `socketcall` would otherwise get past it.
Everything else is allowed. x86-64 and arm64 only (the architectures srt supports).

It uses the kernel's seccomp interface directly through ctypes, with the classic BPF program
written out below, so no library is needed.
"""

from __future__ import annotations

import ctypes
import ctypes.util
import errno
import os
import platform
import socket
import struct

AF_VSOCK = 40
EPERM = 1

# struct seccomp_data: int nr; __u32 arch; __u64 instruction_pointer; __u64 args[6];
OFF_NR, OFF_ARCH, OFF_ARG0 = 0, 4, 16  # args[0]'s low 32 bits on little-endian machines

# (audit arch, socket, io_uring_setup, seccomp, x32 bit or None)
ARCHES = {
    "x86_64": (0xC000003E, 41, 425, 317, 0x40000000),
    "aarch64": (0xC00000B7, 198, 425, 277, None),
}

BPF_LD_W_ABS = 0x20
BPF_JEQ_K = 0x15
BPF_JGE_K = 0x35
BPF_RET_K = 0x06
RET_ALLOW = 0x7FFF0000
RET_ERRNO = 0x00050000
SECCOMP_SET_MODE_FILTER = 1
SECCOMP_FILTER_FLAG_TSYNC = 1
PR_SET_NO_NEW_PRIVS = 38


class Unsupported(Exception):
    pass


def machine() -> str:
    m = platform.machine().lower()
    return {"amd64": "x86_64", "arm64": "aarch64"}.get(m, m)


def program(arch: str) -> bytes:
    """The BPF program, as the kernel's struct sock_filter array."""
    if arch not in ARCHES:
        raise Unsupported(f"no VM-socket filter for {arch}")
    audit, nr_socket, nr_uring, _, x32 = ARCHES[arch]
    deny = RET_ERRNO | EPERM
    # (code, k, jump-if-true label, jump-if-false label); labels resolve to relative offsets
    ins: list[tuple[int, int, str | None, str | None]] = [
        (BPF_LD_W_ABS, OFF_ARCH, None, None),
        (BPF_JEQ_K, audit, "native", "deny"),
        (BPF_LD_W_ABS, OFF_NR, None, None),  # native:
    ]
    if x32 is not None:
        ins.append((BPF_JGE_K, x32, "deny", "next"))
    ins += [
        (BPF_JEQ_K, nr_uring, "deny", "next"),
        (BPF_JEQ_K, nr_socket, "next", "allow"),
        (BPF_LD_W_ABS, OFF_ARG0, None, None),
        (BPF_JEQ_K, AF_VSOCK, "deny", "allow"),
        (BPF_RET_K, RET_ALLOW, None, None),  # allow:
        (BPF_RET_K, deny, None, None),  # deny:
    ]
    where = {"native": 2, "allow": len(ins) - 2, "deny": len(ins) - 1}
    out = b""
    for i, (code, k, jt, jf) in enumerate(ins):

        def rel(label: str | None, here: int = i) -> int:
            if label is None:
                return 0
            target = here + 1 if label == "next" else where[label]
            return target - here - 1

        out += struct.pack("=HBBI", code, rel(jt), rel(jf), k)
    return out


def apply() -> None:
    """Install the filter on this process and all its threads. Raises Unsupported (or OSError)
    when it can't; the caller decides whether that's fatal."""
    if os.name != "posix" or platform.system() != "Linux":
        raise Unsupported("seccomp is Linux-only")
    arch = machine()
    code = program(arch)
    libc_name = ctypes.util.find_library("c") or "libc.so.6"
    libc = ctypes.CDLL(libc_name, use_errno=True)

    class SockFprog(ctypes.Structure):
        _fields_ = (("len", ctypes.c_ushort), ("filter", ctypes.c_void_p))

    buf = ctypes.create_string_buffer(code, len(code))
    prog = SockFprog(len(code) // 8, ctypes.cast(buf, ctypes.c_void_p))
    if libc.prctl(PR_SET_NO_NEW_PRIVS, 1, 0, 0, 0) != 0:
        raise OSError(ctypes.get_errno(), "prctl(PR_SET_NO_NEW_PRIVS) failed")
    nr_seccomp = ARCHES[arch][3]
    rc = libc.syscall(nr_seccomp, SECCOMP_SET_MODE_FILTER, SECCOMP_FILTER_FLAG_TSYNC,
                      ctypes.byref(prog))  # fmt: skip
    if rc != 0:
        err = ctypes.get_errno()
        raise OSError(err, f"seccomp filter not installed: {os.strerror(err)}")


def vm_sockets_refused() -> bool:
    """Whether this process is refused a VM socket (by the filter, or by the kernel)."""
    try:
        s = socket.socket(AF_VSOCK, socket.SOCK_STREAM)
    except OSError as exc:
        return exc.errno in (errno.EPERM, errno.EAFNOSUPPORT, errno.EACCES)
    s.close()
    return False
