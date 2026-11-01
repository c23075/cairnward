"""Per-client API keys for the gateway (FR-GW-2).

Keys are shown once when created; only their SHA-256 is stored, in
<state_dir>/client_keys.json (owner read/write only).
"""

from __future__ import annotations

import hashlib
import json
import secrets
import sys
from datetime import UTC, datetime
from pathlib import Path

from cairnward.config.models import _check_name

PREFIX = "cw-"


def _hash(key: str) -> str:
    return hashlib.sha256(key.encode()).hexdigest()


class ClientKeys:
    def __init__(self, path: Path):
        self.path = path

    @classmethod
    def at(cls, state_dir: Path) -> ClientKeys:
        return cls(state_dir / "client_keys.json")

    def _load(self) -> dict[str, dict[str, str]]:
        if not self.path.exists():
            return {}
        data: dict[str, dict[str, str]] = json.loads(self.path.read_text(encoding="utf-8"))
        return data

    def _save(self, data: dict[str, dict[str, str]]) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        tmp = self.path.with_suffix(".tmp")
        tmp.write_text(json.dumps(data, indent=2, sort_keys=True), encoding="utf-8")
        if sys.platform != "win32":
            tmp.chmod(0o600)
        tmp.replace(self.path)  # atomic

    def add(self, name: str) -> str:
        """Create (or replace) a client's key; returns the key, which is never stored."""
        _check_name(name, "client name")
        key = PREFIX + secrets.token_urlsafe(32)
        data = self._load()
        data[name] = {
            "sha256": _hash(key),
            "created_at": datetime.now(UTC).isoformat(timespec="seconds"),
        }
        self._save(data)
        return key

    def remove(self, name: str) -> bool:
        data = self._load()
        if data.pop(name, None) is None:
            return False
        self._save(data)
        return True

    def names(self) -> dict[str, str]:
        return {n: v["created_at"] for n, v in sorted(self._load().items())}

    def identify(self, key: str | None) -> str | None:
        """The client name for a presented key, or None."""
        if not key or not key.startswith(PREFIX):
            return None
        presented = _hash(key)
        found = None
        for name, entry in self._load().items():
            if secrets.compare_digest(entry["sha256"], presented):
                found = name
        return found
