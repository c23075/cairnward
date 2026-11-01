#!/usr/bin/env python3
"""Stand-in for `cairnward-egress`: an HTTP/CONNECT proxy that logs every request and
decides itself. Allows only plain-HTTP requests to the gateway; refuses everything
else with 403 so the sandbox sees Cairnward's decision. Usage: fake_egress.py PORT LOG GATEWAY_HOSTPORT
"""

from __future__ import annotations

import socket
import sys
import threading
from pathlib import Path

PORT, LOG, GATEWAY = int(sys.argv[1]), Path(sys.argv[2]), sys.argv[3]
lock = threading.Lock()


def log(line: str) -> None:
    with lock, LOG.open("a") as fh:
        fh.write(line + "\n")


def handle(conn: socket.socket) -> None:
    with conn:
        data = b""
        while b"\r\n\r\n" not in data:
            chunk = conn.recv(65536)
            if not chunk:
                return
            data += chunk
        head = data.split(b"\r\n", 1)[0].decode("latin-1")
        method, target, _ = head.split(" ", 2)
        if method == "CONNECT":
            log(f"DENY CONNECT {target}")
            conn.sendall(b"HTTP/1.1 403 Forbidden by cairnward-egress\r\nContent-Length: 0\r\n\r\n")
            return
        hostport = target.split("/")[2] if target.startswith("http://") else ""
        if hostport != GATEWAY:
            log(f"DENY {method} {target}")
            conn.sendall(b"HTTP/1.1 403 Forbidden by cairnward-egress\r\nContent-Length: 0\r\n\r\n")
            return
        log(f"ALLOW {method} {target}")
        host, port = hostport.rsplit(":", 1)
        path = "/" + target.split("/", 3)[3] if target.count("/") >= 3 else "/"
        rest = data.split(b"\r\n", 1)[1]
        with socket.create_connection((host, int(port)), timeout=10) as up:
            up.sendall(f"{method} {path} HTTP/1.0\r\n".encode() + rest)
            while chunk := up.recv(65536):
                conn.sendall(chunk)


srv = socket.socket()
srv.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
srv.bind(("127.0.0.1", PORT))
srv.listen()
while True:
    c, _ = srv.accept()
    threading.Thread(target=handle, args=(c,), daemon=True).start()
