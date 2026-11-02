"""RFC 3161 timestamps for audit checkpoints (D69): third-party proof of when a
checkpoint existed, which the signing key alone can't give (whoever holds it could re-sign
an altered log with earlier dates).

Opt-in: `[anchor] tsa_url = "https://freetsa.org/tsr"` (or any RFC 3161 authority); for
verification also `tsa_ca_file`, the authority's CA certificate. Only the SHA-256 of the
checkpoint is sent (a timestamp query holds the hash, not the data). The token is stored
next to the checkpoint in the anchor repository and checked by `audit verify --anchors`.
Uses the `openssl ts` command.
"""

from __future__ import annotations

import re
import shutil
import subprocess
import tempfile
from pathlib import Path

import httpx


class TimestampError(Exception):
    pass


def _openssl() -> str:
    exe = shutil.which("openssl")
    if exe is None:
        raise TimestampError("openssl isn't installed (needed for RFC 3161 timestamps)")
    return exe


def query(data: bytes) -> bytes:
    """A timestamp query (DER) for the SHA-256 of `data`, asking for the TSA's certificate."""
    with tempfile.TemporaryDirectory() as tmp:
        f = Path(tmp) / "data"
        f.write_bytes(data)
        res = subprocess.run([_openssl(), "ts", "-query", "-data", str(f), "-sha256", "-cert"],  # noqa: S603
                             capture_output=True, check=False)  # fmt: skip
    if res.returncode != 0:
        raise TimestampError(f"openssl ts -query failed: {res.stderr.decode()[:300]}")
    return res.stdout


def request(url: str, data: bytes, *, transport: httpx.BaseTransport | None = None) -> bytes:
    """Ask the authority at `url` to timestamp `data`; returns its reply (DER)."""
    try:
        with httpx.Client(transport=transport, timeout=30) as c:
            resp = c.post(url, content=query(data),
                          headers={"Content-Type": "application/timestamp-query"})  # fmt: skip
    except httpx.HTTPError as exc:
        raise TimestampError(f"the timestamp authority didn't answer: {exc}") from None
    if resp.status_code != 200:
        raise TimestampError(f"the timestamp authority answered {resp.status_code}")
    return resp.content


def verify(data: bytes, reply: bytes, ca_file: Path) -> str:
    """Check the reply covers `data` and is signed by the CA; returns its time."""
    with tempfile.TemporaryDirectory() as tmp:
        d, r = Path(tmp) / "data", Path(tmp) / "reply.tsr"
        d.write_bytes(data)
        r.write_bytes(reply)
        res = subprocess.run([_openssl(), "ts", "-verify", "-data", str(d), "-in", str(r),  # noqa: S603
                              "-CAfile", str(ca_file)], capture_output=True, text=True,
                             check=False)  # fmt: skip
        if res.returncode != 0 or "Verification: OK" not in res.stdout:
            raise TimestampError(f"timestamp doesn't verify: {(res.stdout + res.stderr)[-300:]}")
        text = subprocess.run([_openssl(), "ts", "-reply", "-in", str(r), "-text"],  # noqa: S603
                              capture_output=True, text=True, check=False).stdout  # fmt: skip
    m = re.search(r"Time stamp: (.+)", text)
    return m.group(1).strip() if m else "?"
