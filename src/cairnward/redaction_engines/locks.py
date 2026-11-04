"""Hash-locked requirements for the optional redaction engines (Presidio).

A lock shipped in the repository (`locks/<engine>.txt` for every platform, or
`locks/<engine>-<platform>.txt`) is installed as is, with hashes required: nothing is
resolved on the user's machine. Without one, the installer resolves the pinned
version once and records what it got (trust on first use); export() then copies that
record into the repository so it can be reviewed and committed.
"""

from __future__ import annotations

import platform
import shutil
import sys
from pathlib import Path

LOCK_DIR = Path(__file__).with_name("locks")


def platform_tag() -> str:
    machine = platform.machine().lower().replace("amd64", "x86_64").replace("arm64", "aarch64")
    return f"{sys.platform}-{machine}"


def shipped(engine: str) -> Path | None:
    for name in (f"{engine}-{platform_tag()}.txt", f"{engine}.txt"):
        if (LOCK_DIR / name).is_file():
            return LOCK_DIR / name
    return None


def export(engine: str, installed_lock: Path) -> Path:
    """Copy a lock recorded on this machine into the repository's locks folder."""
    if not installed_lock.is_file():
        raise FileNotFoundError(f"no recorded lock at {installed_lock}: install {engine} first")
    LOCK_DIR.mkdir(exist_ok=True)
    dest = LOCK_DIR / f"{engine}-{platform_tag()}.txt"
    shutil.copyfile(installed_lock, dest)
    return dest
