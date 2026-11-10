"""Merkle tree over the audit log (RFC 9162 §2.1, as in Certificate Transparency; ADR-0008).

The leaves are the audit records' own hashes, in sequence order: leaf n (0-based) is record
seq n+1. A checkpoint that signs the tree's root over the first `size` records commits to every
one of them, so a single record can then be checked against the checkpoint with an inclusion
proof of about log2(size) hashes, without the rest of the log. A consistency proof shows that a
later checkpoint's tree extends an earlier one's, i.e. that nothing anchored was rewritten.

Hashing follows RFC 9162: SHA-256, a 0x00 prefix for leaves and 0x01 for interior nodes (so a
leaf can't be passed off as a node), and the empty tree is SHA-256 of nothing. Hashes travel as
lowercase hex. Everything here is pure computation; nothing reads the log or the network.
"""

from __future__ import annotations

import hashlib
from collections.abc import Sequence

EMPTY_ROOT = hashlib.sha256(b"").hexdigest()


class ProofError(ValueError):
    """A proof or its inputs are malformed (as opposed to well formed and not matching)."""


def _h(data: bytes) -> bytes:
    return hashlib.sha256(data).digest()


def _bytes(value: str) -> bytes:
    try:
        raw = bytes.fromhex(value)
    except ValueError:
        raise ProofError(f"not a hex hash: {value[:20]!r}") from None
    if len(raw) != 32:
        raise ProofError("a hash must be 32 bytes (64 hex characters)")
    return raw


def leaf_hash(record_hash: str) -> bytes:
    """The tree's leaf for one audit record, from the record's own hash."""
    return _h(b"\x00" + _bytes(record_hash))


def _node(left: bytes, right: bytes) -> bytes:
    return _h(b"\x01" + left + right)


def _split(n: int) -> int:
    """The largest power of two smaller than n (n >= 2)."""
    k = 1
    while k * 2 < n:
        k *= 2
    return k


def _root(leaves: Sequence[bytes]) -> bytes:
    n = len(leaves)
    if n == 0:
        return _h(b"")
    if n == 1:
        return leaves[0]
    k = _split(n)
    return _node(_root(leaves[:k]), _root(leaves[k:]))


def root(record_hashes: Sequence[str]) -> str:
    """The root over these record hashes, in order (RFC 9162 MTH)."""
    return _root([leaf_hash(h) for h in record_hashes]).hex()


def _inclusion(index: int, leaves: Sequence[bytes]) -> list[bytes]:
    n = len(leaves)
    if n <= 1:
        return []
    k = _split(n)
    if index < k:
        return [*_inclusion(index, leaves[:k]), _root(leaves[k:])]
    return [*_inclusion(index - k, leaves[k:]), _root(leaves[:k])]


def inclusion_proof(index: int, record_hashes: Sequence[str]) -> list[str]:
    """The audit path for leaf `index` (0-based) in the tree over `record_hashes`."""
    if not 0 <= index < len(record_hashes):
        raise ProofError(f"leaf {index} is outside a tree of {len(record_hashes)}")
    return [h.hex() for h in _inclusion(index, [leaf_hash(r) for r in record_hashes])]


def verify_inclusion(record_hash: str, index: int, size: int, proof: Sequence[str],
                     expected_root: str) -> bool:  # fmt: skip
    """RFC 9162 §2.1.3.2: does `record_hash` sit at leaf `index` of the tree with this root?"""
    if not 0 <= index < size:
        raise ProofError(f"leaf {index} is outside a tree of {size}")
    fn, sn = index, size - 1
    r = leaf_hash(record_hash)
    for p in (_bytes(x) for x in proof):
        if sn == 0:
            return False  # the proof is longer than the tree allows
        if fn % 2 == 1 or fn == sn:
            r = _node(p, r)
            if fn % 2 == 0:
                while fn % 2 == 0 and fn != 0:
                    fn >>= 1
                    sn >>= 1
        else:
            r = _node(r, p)
        fn >>= 1
        sn >>= 1
    return sn == 0 and r == _bytes(expected_root)


def _subproof(m: int, leaves: Sequence[bytes], complete: bool) -> list[bytes]:
    n = len(leaves)
    if m == n:
        return [] if complete else [_root(leaves)]
    k = _split(n)
    if m <= k:
        return [*_subproof(m, leaves[:k], complete), _root(leaves[k:])]
    return [*_subproof(m - k, leaves[k:], False), _root(leaves[:k])]


def consistency_proof(old_size: int, record_hashes: Sequence[str]) -> list[str]:
    """Proof that the tree over the first `old_size` hashes is a prefix of the tree over all."""
    n = len(record_hashes)
    if not 0 <= old_size <= n:
        raise ProofError(f"old size {old_size} is outside 0..{n}")
    if old_size in (0, n):
        return []
    leaves = [leaf_hash(r) for r in record_hashes]
    return [h.hex() for h in _subproof(old_size, leaves, True)]


def verify_consistency(old_size: int, new_size: int, old_root: str, new_root: str,
                       proof: Sequence[str]) -> bool:  # fmt: skip
    """RFC 9162 §2.1.4.2: is the tree (new_size, new_root) an extension of (old_size, old_root)?"""
    if not 0 <= old_size <= new_size:
        raise ProofError(f"old size {old_size} is larger than new size {new_size}")
    path = [_bytes(x) for x in proof]
    if old_size == 0:
        return not path  # the empty tree is a prefix of every tree
    if old_size == new_size:
        return not path and _bytes(old_root) == _bytes(new_root)
    if old_size & (old_size - 1) == 0:  # a power of two: the old root is the first node
        path = [_bytes(old_root), *path]
    if not path:
        return False
    fn, sn = old_size - 1, new_size - 1
    while fn % 2 == 1:
        fn >>= 1
        sn >>= 1
    fr = sr = path[0]
    for c in path[1:]:
        if sn == 0:
            return False
        if fn % 2 == 1 or fn == sn:
            fr = _node(c, fr)
            sr = _node(c, sr)
            if fn % 2 == 0:
                while fn % 2 == 0 and fn != 0:
                    fn >>= 1
                    sn >>= 1
        else:
            sr = _node(sr, c)
        fn >>= 1
        sn >>= 1
    return sn == 0 and fr == _bytes(old_root) and sr == _bytes(new_root)
