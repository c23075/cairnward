"""ADR-0020 stage 4: an independent Rust verifier (`cairnward-audit-rs`) agrees with the Python
audit log on every record hash, the chain and the Merkle roots, and catches the same tampering.
Runs when the binary is built; skips otherwise. CI builds it."""

import json
import os
import subprocess
from pathlib import Path
from typing import Any

import pytest
from hypothesis import HealthCheck, given, settings
from hypothesis import strategies as st

from cairnward.audit import AuditLog, merkle
from cairnward.audit.log import FORBIDDEN_DETAIL_KEYS
from cairnward.config.paths import Paths

ROOT = Path(__file__).resolve().parent.parent


def _binary() -> Path | None:
    if found := os.environ.get("CAIRNWARD_AUDIT_RS"):
        return Path(found)
    for profile in ("release", "debug"):
        exe = ROOT / "rust" / "target" / profile / "cairnward-audit-rs"
        for candidate in (exe, exe.with_suffix(".exe")):
            if candidate.is_file():
                return candidate
    return None


BINARY = _binary()
pytestmark = pytest.mark.skipif(BINARY is None, reason="Rust audit binary not built")


def rust(*args: str) -> subprocess.CompletedProcess[str]:
    assert BINARY is not None
    return subprocess.run([str(BINARY), *args], capture_output=True, text=True, timeout=60,
                          check=False)  # fmt: skip


TEXT = st.text(st.characters(codec="utf-8"), max_size=40)  # no lone surrogates
FLOATS = st.floats(allow_nan=False, allow_infinity=False) | st.sampled_from(
    [0.1, 1e-05, 1e16, 1.5e16, 1e22, 5e-324, -0.0, 123456789012345.6, 0.0001, 1e-4, 9.999e15]
)
SCALARS = st.none() | st.booleans() | st.integers(-(2**63), 2**63 - 1) | FLOATS | TEXT
KEYS = TEXT.filter(lambda k: k.lower() not in FORBIDDEN_DETAIL_KEYS)
DETAILS = st.dictionaries(KEYS, st.recursive(
    SCALARS, lambda kids: st.lists(kids, max_size=3) | st.dictionaries(KEYS, kids, max_size=3),
    max_leaves=8), max_size=5)  # fmt: skip


@settings(
    max_examples=40, deadline=None, suppress_health_check=[HealthCheck.function_scoped_fixture]
)
@given(st.lists(st.tuples(DETAILS, TEXT), min_size=1, max_size=25))
def test_rust_agrees_on_every_record_and_root(tmp_path: Path, batch: list[tuple[Any, str]]) -> None:
    folder = tmp_path / f"run{len(list(tmp_path.iterdir()))}"
    paths = Paths(folder / "c", folder / "d", folder / "s")
    with AuditLog.open(paths) as log:
        for details, target in batch:
            log.append("tool.call", actor="agent:t", target=target, details=details)
        seq, head = log.head()
        mirror = log.mirror
        assert mirror is not None
        out = rust("verify", str(mirror))
        assert out.returncode == 0, out.stdout + out.stderr
        assert out.stdout.strip() == f"OK {seq} {seq}:{head}"
        for size in {1, seq // 2 or 1, seq}:
            assert rust("root", str(mirror), str(size)).stdout.strip() == merkle.root(
                log.hashes(size)
            )


def test_rust_catches_the_tampering_python_catches(tmp_path: Path) -> None:
    paths = Paths(tmp_path / "c", tmp_path / "d", tmp_path / "s")
    with AuditLog.open(paths) as log:
        for i in range(5):
            log.append("tool.call", actor="agent:t", details={"n": i, "t": 0.5})
        mirror = log.mirror
        assert mirror is not None
    lines = mirror.read_text(encoding="utf-8").splitlines()

    def check(edited: list[str]) -> str:
        f = tmp_path / "edited.jsonl"
        f.write_text("\n".join(edited) + "\n", encoding="utf-8")
        out = rust("verify", str(f))
        assert out.returncode == 1
        return out.stdout

    changed = json.loads(lines[2])
    changed["details"]["n"] = 99
    assert "record 3: contents changed" in check([*lines[:2], json.dumps(changed), *lines[3:]])
    assert "record 3: missing" in check([*lines[:2], *lines[3:]])
    swapped = [lines[0], lines[2], lines[1], *lines[3:]]
    assert "does not link" in check(swapped)


def test_rust_checks_an_inclusion_proof(tmp_path: Path) -> None:
    paths = Paths(tmp_path / "c", tmp_path / "d", tmp_path / "s")
    with AuditLog.open(paths) as log:
        for i in range(9):
            log.append("tool.call", actor="agent:t", details={"n": i})
        hashes = log.hashes(9)
        rec = log.at(4)
        assert rec is not None
        from dataclasses import asdict

        checkpoint = {"payload": {"seq": 9, "root": merkle.root(hashes)}, "sig": ""}
        bundle: dict[str, Any] = {
            "format": "cairnward-inclusion-proof-1", "record": asdict(rec), "index": 3,
            "size": 9, "proof": merkle.inclusion_proof(3, hashes), "checkpoint": checkpoint,
        }  # fmt: skip
    proof = tmp_path / "proof.json"
    proof.write_text(json.dumps(bundle), encoding="utf-8")
    out = rust("check-proof", str(proof))
    assert out.returncode == 0 and "OK record 4 is in the checkpoint at seq 9" in out.stdout
    bundle["record"]["action"] = "vault.purge"
    proof.write_text(json.dumps(bundle), encoding="utf-8")
    assert "edited" in rust("check-proof", str(proof)).stdout
