"""Merkle tree for audit checkpoints (audit/merkle.py): RFC 6962 vectors and proof properties."""

import hashlib

import pytest
from hypothesis import given, settings
from hypothesis import strategies as st

from cairnward.audit import merkle

# RFC 6962 / Certificate Transparency reference data: these leaf inputs, and the roots of the
# trees over their first n leaves (published with the CT reference implementations).
_LEAF_DATA = [b"", b"\x00", b"\x10", b"\x20\x21", b"\x30\x31", b"\x40\x41\x42\x43",
              bytes(range(0x50, 0x58)), bytes(range(0x60, 0x70))]  # fmt: skip
_ROOTS = {
    1: "6e340b9cffb37a989ca544e6bb780a2c78901d3fb33738768511a30617afa01d",
    2: "fac54203e7cc696cf0dfcb42c92a1d9dbaf70ad9e621f4bd8d98662f00e3c125",
    3: "aeb6bcfe274b70a14fb067a5e5578264db0fa9b51af5e0ba159158f329e06e77",
    4: "d37ee418976dd95753c1c73862b9398fa2a2cf9b4ff0fdfe8b30cd95209614b7",
    5: "4e3bbb1f7b478dcfe71fb631631519a3bca12c9aefca1612bfce4c13a86264d4",
    6: "76e67dadbcdf1e10e1b74ddc608abd2f98dfb16fbce75277b5232a127f2087ef",
    7: "ddb89be403809e325750d3d263cd78929c2942b7942a34b77e122c9594a74c8c",
    8: "5dc9da79a70659a9ad559cb701ded9a2ab9d823aad2f4960cfe370eff4604328",
}


def _hashes(n: int) -> list[str]:
    return [hashlib.sha256(str(i).encode()).hexdigest() for i in range(n)]


def test_tree_hashing_matches_rfc6962_vectors() -> None:
    leaves = [hashlib.sha256(b"\x00" + d).digest() for d in _LEAF_DATA]
    for n, expected in _ROOTS.items():
        assert merkle._root(leaves[:n]).hex() == expected
    assert merkle.root([]) == merkle.EMPTY_ROOT == hashlib.sha256(b"").hexdigest()


def test_leaves_and_nodes_are_domain_separated() -> None:
    a, b = _hashes(2)
    node = merkle.root([a, b])
    # a two-leaf root can't be presented as a single leaf with the same root
    assert merkle.root([node]) != node


@settings(max_examples=60, deadline=None)
@given(st.integers(min_value=1, max_value=70), st.data())
def test_every_inclusion_proof_verifies_and_tampering_fails(n: int, data: st.DataObject) -> None:
    hashes = _hashes(n)
    top = merkle.root(hashes)
    i = data.draw(st.integers(min_value=0, max_value=n - 1))
    proof = merkle.inclusion_proof(i, hashes)
    assert merkle.verify_inclusion(hashes[i], i, n, proof, top)
    other = hashlib.sha256(b"forged").hexdigest()
    assert not merkle.verify_inclusion(other, i, n, proof, top)  # a different record
    if n > 1:
        assert not merkle.verify_inclusion(hashes[i], (i + 1) % n, n, proof, top)  # moved
        bad = [*proof]
        bad[0] = other
        assert not merkle.verify_inclusion(hashes[i], i, n, bad, top)  # a changed path
        assert not merkle.verify_inclusion(hashes[i], i, n, proof[:-1], top)  # too short
    assert not merkle.verify_inclusion(hashes[i], i, n, [*proof, other], top)  # too long


@settings(max_examples=60, deadline=None)
@given(st.integers(min_value=1, max_value=70), st.data())
def test_every_consistency_proof_verifies_and_rewrites_fail(n: int, data: st.DataObject) -> None:
    hashes = _hashes(n)
    m = data.draw(st.integers(min_value=0, max_value=n))
    old_root, new_root = merkle.root(hashes[:m]), merkle.root(hashes)
    proof = merkle.consistency_proof(m, hashes)
    assert merkle.verify_consistency(m, n, old_root, new_root, proof)
    if m > 0:
        # history before the old checkpoint rewritten: the new tree no longer extends the old one
        rewritten = [hashlib.sha256(b"x").hexdigest(), *hashes[1:]]
        assert not merkle.verify_consistency(m, n, old_root, merkle.root(rewritten),
                                             merkle.consistency_proof(m, rewritten))  # fmt: skip
    if 0 < m < n:
        assert not merkle.verify_consistency(m, n, new_root, new_root, proof)


def test_malformed_inputs_are_refused() -> None:
    hashes = _hashes(3)
    with pytest.raises(merkle.ProofError):
        merkle.inclusion_proof(3, hashes)
    with pytest.raises(merkle.ProofError):
        merkle.verify_inclusion("zz", 0, 3, [], merkle.root(hashes))
    with pytest.raises(merkle.ProofError):
        merkle.verify_consistency(4, 3, merkle.root(hashes), merkle.root(hashes), [])
