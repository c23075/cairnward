"""Model downloads through egress, verified before use (spec §5.6, FR-SUP-2, T-15).

`cairnward models pull <url> --sha256 <hex>` downloads one model file:
- only GGUF or safetensors (no pickle-based formats that run code when loaded);
- through cairnward-egress with a short-lived session, so every host on the way
  (including CDN redirects) is checked against the network policy and audited;
- written to a private partial file, checked for the format's header, and kept only if
  its SHA-256 matches the one you gave (from the model's publisher);
- recorded in the audit log (`model.download`) and in models/manifest.json.
"""

from __future__ import annotations

import hashlib
import json
import struct
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from typing import Any
from urllib.parse import unquote, urlsplit

import httpx

ALLOWED = (".gguf", ".safetensors")
MAX_SAFETENSORS_HEADER = 100 * 1024 * 1024


class DownloadError(Exception):
    pass


@dataclass(frozen=True)
class Downloaded:
    path: Path
    sha256: str
    size: int


def models_dir(data_dir: Path) -> Path:
    d = data_dir / "models"
    d.mkdir(parents=True, exist_ok=True)
    d.chmod(0o700)
    return d


def file_name(url: str, name: str | None) -> str:
    candidate = name or unquote(Path(urlsplit(url).path).name)
    if not candidate or "/" in candidate or candidate.startswith("."):
        raise DownloadError("give the file a name with --name")
    if not candidate.lower().endswith(ALLOWED):
        raise DownloadError(f"only {' and '.join(ALLOWED)} files are accepted, not '{candidate}'")
    return candidate


def check_header(path: Path, name: str) -> None:
    """`name` is the final file name (its extension says which format to expect)."""
    with path.open("rb") as fh:
        head = fh.read(8)
        if name.lower().endswith(".gguf"):
            if head[:4] != b"GGUF":
                raise DownloadError("not a GGUF file (bad magic number)")
            return
        if len(head) < 8:
            raise DownloadError("not a safetensors file (too short)")
        (n,) = struct.unpack("<Q", head)
        if not 2 <= n <= MAX_SAFETENSORS_HEADER:
            raise DownloadError("not a safetensors file (bad header length)")
        try:
            json.loads(fh.read(n))
        except ValueError:
            raise DownloadError("not a safetensors file (header isn't JSON)") from None


def download(
    url: str,
    sha256: str,
    dest: Path,
    *,
    name: str | None = None,
    proxy: str | None = None,
    progress: Callable[[int, int | None], None] | None = None,
    transport: httpx.BaseTransport | None = None,
) -> Downloaded:
    expected = sha256.lower().strip()
    if len(expected) != 64 or any(c not in "0123456789abcdef" for c in expected):
        raise DownloadError("--sha256 must be 64 hexadecimal characters")
    if urlsplit(url).scheme not in ("https", "http"):
        raise DownloadError("only http(s) URLs")
    dest.mkdir(parents=True, exist_ok=True)
    target = dest / file_name(url, name)
    part = dest / f".{target.name}.part"
    if target.exists():
        raise DownloadError(f"{target.name} already exists")
    digest, size = hashlib.sha256(), 0
    try:
        with httpx.Client(proxy=proxy, transport=transport, follow_redirects=True,
                          timeout=httpx.Timeout(60, read=300), trust_env=False) as client, \
                client.stream("GET", url) as resp:  # fmt: skip
            if resp.status_code != 200:
                raise DownloadError(f"HTTP {resp.status_code} from {resp.url.host}: "
                                    f"{resp.read()[:200].decode('utf-8', 'replace')}")  # fmt: skip
            total = int(resp.headers.get("content-length") or 0) or None
            with part.open("wb") as fh:
                part.chmod(0o600)
                for chunk in resp.iter_bytes(1024 * 1024):
                    fh.write(chunk)
                    digest.update(chunk)
                    size += len(chunk)
                    if progress:
                        progress(size, total)
        got = digest.hexdigest()
        if got != expected:
            raise DownloadError(f"SHA-256 mismatch: got {got}, expected {expected}; file deleted")
        check_header(part, target.name)
        part.rename(target)
    except httpx.HTTPError as exc:
        raise DownloadError(f"download failed: {exc}") from None
    finally:
        part.unlink(missing_ok=True)
    _record(dest, target.name, url, got, size)
    return Downloaded(target, got, size)


def _record(dest: Path, name: str, url: str, sha: str, size: int) -> None:
    manifest = dest / "manifest.json"
    data = json.loads(manifest.read_text()) if manifest.exists() else {}
    data[name] = {"sha256": sha, "size": size, "url": url}
    manifest.write_text(json.dumps(data, indent=2, sort_keys=True))


def listed(dest: Path) -> dict[str, dict[str, Any]]:
    manifest = dest / "manifest.json"
    data: dict[str, dict[str, object]] = (
        json.loads(manifest.read_text()) if manifest.exists() else {}
    )
    return {n: info for n, info in data.items() if (dest / n).exists()}
