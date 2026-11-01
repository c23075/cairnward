#!/usr/bin/env python3
"""Runs INSIDE the sandbox and tries everything a misbehaving agent might try.

Prints one JSON object: {check_name: {"ok": bool, "detail": str}} where ok means
"the sandbox behaved as Cairnward requires" (blocked when it should block, worked
when it should work). Standard library only.
"""

from __future__ import annotations

import json
import os
import socket
import sys
import urllib.request
from pathlib import Path

GATEWAY = os.environ["SPIKE_GATEWAY_URL"]  # e.g. http://127.0.0.1:18484/
WORKSPACE = Path(os.environ["SPIKE_WORKSPACE"])
SECRET = Path(os.environ["SPIKE_SECRET_FILE"])
OUTSIDE = Path(os.environ["SPIKE_OUTSIDE_FILE"])
HOST_SOCKET = os.environ["SPIKE_HOST_SOCKET"]
ALLOWED_URL = os.environ.get("SPIKE_ALLOWED_URL", "")
DENIED_URL = os.environ.get("SPIKE_DENIED_URL", "https://example.com/")

results: dict[str, dict[str, object]] = {}


def check(name: str, should_work: bool, fn) -> None:  # type: ignore[no-untyped-def]
    try:
        detail = fn()
        worked = True
    except Exception as exc:
        detail = f"{type(exc).__name__}: {exc}"
        worked = False
    results[name] = {"ok": worked == should_work, "worked": worked, "detail": str(detail)[:200]}


def via_proxy(url: str) -> str:
    with urllib.request.urlopen(url, timeout=10) as resp:  # uses *_proxy env vars
        return f"HTTP {resp.status}"


def direct(url: str) -> str:
    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
    with opener.open(url, timeout=5) as resp:
        return f"HTTP {resp.status}"


def tcp(host: str, port: int) -> str:
    with socket.create_connection((host, port), timeout=5):
        return "connected"


def write(path: Path) -> str:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("probe\n")
    return f"wrote {path}"


def unix_socket_create() -> str:
    s = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    s.close()
    return "created AF_UNIX socket"


def unix_socket_connect() -> str:
    s = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    s.settimeout(3)
    s.connect(HOST_SOCKET)
    s.close()
    return "connected to host socket"


def new_user_namespace() -> str:
    os.unshare(os.CLONE_NEWUSER)  # type: ignore[attr-defined]
    return "created user namespace"


check("gateway_via_proxy", True, lambda: via_proxy(GATEWAY))
check("gateway_direct_bypassing_proxy", False, lambda: direct(GATEWAY))
check("internet_direct_tcp", False, lambda: tcp("1.1.1.1", 443))
check("dns_lookup", False, lambda: socket.getaddrinfo("example.com", 443))
check("denied_domain_via_proxy", False, lambda: via_proxy(DENIED_URL))
if ALLOWED_URL:
    check("allowed_domain_via_proxy", True, lambda: via_proxy(ALLOWED_URL))
check("read_secret_file", False, lambda: SECRET.read_text())
check("write_in_workspace", True, lambda: write(WORKSPACE / "agent-output.txt"))
check("write_outside_workspace", False, lambda: write(OUTSIDE))
check("write_git_hook", False, lambda: write(WORKSPACE / ".git" / "hooks" / "pre-commit"))
check("write_git_config", False, lambda: write(WORKSPACE / ".git" / "config"))
check("unix_socket_create", False, unix_socket_create)
check("unix_socket_connect_host", False, unix_socket_connect)
check("new_user_namespace", False, new_user_namespace)

json.dump(results, sys.stdout, indent=2)
print()
