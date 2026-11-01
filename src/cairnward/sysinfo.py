"""Small system facts (Linux /proc; empty elsewhere)."""

from __future__ import annotations

from pathlib import Path

GB = 1024**3


def meminfo() -> dict[str, int]:
    """/proc/meminfo in bytes (MemTotal, MemAvailable, ...); {} where unavailable."""
    try:
        out = {}
        for line in Path("/proc/meminfo").read_text().splitlines():
            key, _, rest = line.partition(":")
            out[key] = int(rest.split()[0]) * 1024
        return out
    except (OSError, ValueError, IndexError):
        return {}
