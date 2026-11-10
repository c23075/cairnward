"""Database encryption (D80) with a real `serve`, including the core in the real sandbox.

`serve` can't be asked for a passphrase here (no terminal), so the key comes from a
`[storage] key` reference, the unattended setup. In isolated mode the outer process hands the
key to the sandboxed core; the core must open the encrypted databases, and the key must not be
readable from the core's /proc/<pid>/environ afterwards (threat-model review 2026-11-10, R1).
"""

import os
import subprocess
import sys
from pathlib import Path

import pytest

from tests.isolation.harness import Cairnward

pytestmark = pytest.mark.skipif(not sys.platform.startswith("linux"), reason="Linux only")
sqlcipher3 = pytest.importorskip("sqlcipher3", reason="SQLCipher driver (Linux x86-64 only)")


def _encrypt(cw: Cairnward) -> bytes:
    from cairnward.audit import AuditLog
    from cairnward.store import Vault, dbkey
    from cairnward.store.db import convert

    with Vault.open(cw.paths) as vault:
        conv = vault.create_conversation(environment="default", client="t", title="secret title")
        vault.add_message(conv, {"role": "user", "content": "hello"})
    AuditLog.open(cw.paths).close()
    key = dbkey.begin(cw.paths, "a long passphrase")
    for name in dbkey.DATABASES:
        convert(cw.paths.data_dir / name, key, encrypt=True)
    dbkey.commit(cw.paths, key)
    dbkey.forget(cw.paths)
    (cw.paths.data_dir / "audit.jsonl").unlink(missing_ok=True)
    return key


def _core_pids() -> list[int]:
    out = subprocess.run(["pgrep", "-f", "cairnward _core"], capture_output=True, text=True)
    return [int(p) for p in out.stdout.split()]


@pytest.mark.parametrize("mode", ["isolated", "off"])
def test_serve_with_encrypted_databases(tmp_path: Path, mode: str) -> None:
    cw = Cairnward(tmp_path, mode, main='[storage]\nkey = "env:CW_TEST_DB_KEY"\n')
    key = _encrypt(cw)
    cw.env["CW_TEST_DB_KEY"] = key.hex()
    with cw:
        out = cw.cw("vault", "list", check=True).stdout
        assert "secret title" in out
        link = cw.cw("dashboard", "--no-open", check=True).stdout.splitlines()[1]
        import httpx

        with httpx.Client(base_url=f"http://127.0.0.1:{cw.dash}", trust_env=False) as web:
            assert web.get(link.split(str(cw.dash), 1)[1]).status_code == 303
            convs = web.get("/api/vault").json()  # served by the core, from the encrypted vault
            assert any(c["title"] == "secret title" for c in convs), convs
            assert any("[storage] key" in w for w in web.get("/api/session").json()["warnings"])
        if cw.core_isolated:
            pids = _core_pids()
            assert pids, "the sandboxed core isn't running"
            for pid in pids:
                environ = Path(f"/proc/{pid}/environ").read_bytes()
                assert key.hex().encode() not in environ, "the key is readable in /proc"
    assert not (cw.paths.data_dir / "audit.jsonl").exists()
    for name in ("vault.db", "keys.db", "audit.db"):
        assert b"secret title" not in (cw.paths.data_dir / name).read_bytes()
    assert os.environ.get("CW_TEST_DB_KEY") is None
