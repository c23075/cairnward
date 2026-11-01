"""Per-conversation encryption with age (pyrage)."""

from __future__ import annotations

import hashlib

from pyrage import decrypt, encrypt, passphrase, x25519


class DecryptionError(Exception):
    pass


def new_key() -> bytes:
    """A new conversation key: an age X25519 identity, as bytes."""
    return str(x25519.Identity.generate()).encode()


def seal(key: bytes, plaintext: bytes) -> bytes:
    identity = x25519.Identity.from_str(key.decode())
    return bytes(encrypt(plaintext, [identity.to_public()]))


def open_sealed(key: bytes, ciphertext: bytes) -> bytes:
    identity = x25519.Identity.from_str(key.decode())
    try:
        return bytes(decrypt(ciphertext, [identity]))
    except Exception as exc:  # pyrage raises its own error types
        raise DecryptionError("cannot decrypt with this conversation's key") from exc


def digest(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def wrap_key(key: bytes, secret: str) -> bytes:
    """Encrypt a conversation key with a passphrase (age scrypt recipient)."""
    return bytes(passphrase.encrypt(key, secret))


def unwrap_key(wrapped: bytes, secret: str) -> bytes:
    try:
        return bytes(passphrase.decrypt(wrapped, secret))
    except Exception as exc:  # pyrage's DecryptError
        raise DecryptionError("wrong passphrase") from exc
