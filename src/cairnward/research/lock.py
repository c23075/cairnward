"""Locking a literature library with a passphrase (FR-ENC-1, T-24).

A library's documents, chunks, embeddings and GROBID metadata become one file encrypted with
a new key, stored only wrapped with the conversations' keys, so the recovery key covers it
too. The library's name, folder and environment stay readable. While locked it can't be
searched, asked or indexed, and models don't see it. Unlocking removes the lock and restores
it; there's no timed unlock as for conversations, because search needs the rows in research.db.

The source PDFs are in your own folder: locking doesn't touch them. Earlier plaintext isn't
erased from free disk space, and stays in old backups.

The key is saved first and removed last, and the encrypted file is complete before anything
is deleted, so a crash at any point can be finished by locking or unlocking again.
"""

from __future__ import annotations

import base64
import contextlib
import json
import re
from pathlib import Path
from typing import Any

from cairnward.artifacts import _pack, _remove_plaintext, _unpack, _write_atomic, remove_tree
from cairnward.research.engines.paperqa import cache_dir
from cairnward.research.grobid import meta_dir
from cairnward.research.library import Research
from cairnward.store import crypto
from cairnward.store.vault import VaultError


def item_id(name: str) -> str:
    return f"library:{name}"


def _paperqa_cache(name: str) -> Path:
    return cache_dir() / re.sub(r"[^A-Za-z0-9._-]", "_", name)


def lock_library(vault: Any, research: Research, data_dir: Path, name: str, secret: str) -> None:
    lib = research.find(name)
    sealed = research.locked_path(name)
    if sealed.exists() and vault.is_locked(item_id(name)):  # a crash after sealing: finish it
        _wipe(research, data_dir, name)
        return
    if lib.locked:
        raise VaultError("already locked")
    if vault.is_locked(item_id(name)):  # a crash after saving the key: same passphrase, go on
        key = vault.open_item(item_id(name), secret)
    else:
        key = vault.lock_item(item_id(name), secret)
    rows = research.export_rows(lib)
    meta = meta_dir(data_dir, name)
    if meta.is_dir():
        rows["meta"] = base64.b64encode(_pack(meta)).decode()
    sealed.parent.mkdir(parents=True, exist_ok=True)
    _write_atomic(sealed, crypto.seal(key, json.dumps(rows).encode()))
    _wipe(research, data_dir, name)


def _wipe(research: Research, data_dir: Path, name: str) -> None:
    research.wipe_rows(research.find(name))
    meta = meta_dir(data_dir, name)
    if meta.is_dir():
        _remove_plaintext(meta)
        with contextlib.suppress(OSError):
            meta.rmdir()
    cache = _paperqa_cache(name)
    if cache.is_dir():  # PaperQA2's own copy of the papers' text, rebuilt when needed
        remove_tree(cache)


def unlock_library(vault: Any, research: Research, data_dir: Path, name: str, secret: str) -> None:
    item = item_id(name)
    key = vault.open_item(item, secret)
    sealed = research.locked_path(name)
    rows = json.loads(crypto.open_sealed(key, sealed.read_bytes()))
    research.import_rows(research.find(name), rows)
    if rows.get("meta"):
        meta = meta_dir(data_dir, name)
        meta.mkdir(parents=True, exist_ok=True)
        _unpack(base64.b64decode(rows["meta"]), meta)
    sealed.unlink()
    vault.remove_item(item)
