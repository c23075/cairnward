"""Google Drive (or any rclone remote) as a read-only library source (FR-SYNC-2).

You set up the remote yourself with `rclone config` (rclone holds the Google sign-in;
Cairnward never sees it); give it `scope = drive.readonly`, which Cairnward checks. Then

    cairnward library drive thesis --remote gdrive:Papers/Thesis --preview
    cairnward library drive thesis --remote gdrive:Papers/Thesis

lists, then copies, the PDFs (only new or changed ones) into `<library folder>/drive/`
and indexes them. Nothing is ever written to the remote: rclone runs `lsjson` and `copy`
from the remote only. Files removed from Drive are reported and kept, unless `--prune`
(which deletes them from `drive/` only).

The connection is checked by the policy engine first, as a `net.connect` to Google's API
host in the library's environment (refused where the network is off; asks where it says
ask), and each run is audited. rclone connects itself, so its traffic is not counted in
the egress report.
"""

from __future__ import annotations

import json
import shutil
import subprocess
from collections.abc import Callable
from pathlib import Path
from typing import Any

HOST = "www.googleapis.com:443"
MAX_SIZE = "200M"
Runner = Callable[[list[str]], subprocess.CompletedProcess[str]]


class DriveError(Exception):
    pass


def _run(args: list[str]) -> subprocess.CompletedProcess[str]:
    exe = shutil.which("rclone")
    if exe is None:
        raise DriveError("rclone isn't installed (e.g. sudo apt install rclone), then set up a "
                         "remote with: rclone config")  # fmt: skip
    return subprocess.run([exe, *args], capture_output=True, text=True, timeout=3600,  # noqa: S603
                          check=False)  # fmt: skip


def check_remote(remote: str, run: Runner = _run) -> list[str]:
    """Warnings about the remote's setup (it must exist; Drive should be read-only)."""
    name = remote.split(":", 1)[0]
    if ":" not in remote or not name:
        raise DriveError("--remote looks like name:path, e.g. gdrive:Papers/Thesis")
    res = run(["config", "dump"])
    if res.returncode != 0:
        raise DriveError(f"rclone config dump failed: {res.stderr.strip()[:300]}")
    try:
        conf = json.loads(res.stdout or "{}").get(name)
    except ValueError:
        conf = None
    if not isinstance(conf, dict):
        raise DriveError(f"no rclone remote '{name}': set one up with: rclone config")
    warnings = []
    if conf.get("type") == "drive" and conf.get("scope") != "drive.readonly":
        warnings.append(f"remote '{name}' can change your Drive (scope = "
                        f"{conf.get('scope') or 'drive'}); Cairnward only reads, but a "
                        "read-only remote (scope = drive.readonly) is safer")  # fmt: skip
    return warnings


def listing(remote: str, run: Runner = _run) -> list[dict[str, Any]]:
    res = run(["lsjson", remote, "--recursive", "--files-only", "--include", "*.pdf",
               "--include", "*.PDF"])  # fmt: skip
    if res.returncode != 0:
        raise DriveError(f"rclone lsjson failed: {res.stderr.strip()[:300]}")
    try:
        rows = json.loads(res.stdout or "[]")
    except ValueError:
        raise DriveError("rclone gave an unreadable listing") from None
    return [
        r for r in rows if isinstance(r, dict) and str(r.get("Path", "")).lower().endswith(".pdf")
    ]


def plan(remote_files: list[dict[str, Any]], local: Path) -> dict[str, list[str]]:
    """What a mirror would do: new, changed (size differs), unchanged, gone from Drive."""
    out: dict[str, list[str]] = {"new": [], "changed": [], "unchanged": [], "gone": []}
    names = set()
    for r in remote_files:
        rel = str(r["Path"])
        names.add(rel)
        target = local / rel
        if not target.exists():
            out["new"].append(rel)
        elif target.stat().st_size != int(r.get("Size", -1)):
            out["changed"].append(rel)
        else:
            out["unchanged"].append(rel)
    if local.is_dir():
        out["gone"] = sorted(p.relative_to(local).as_posix() for p in local.rglob("*")
                             if p.is_file() and p.suffix.lower() == ".pdf"
                             and p.relative_to(local).as_posix() not in names)  # fmt: skip
    return out


def mirror(remote: str, local: Path, *, prune: bool = False, run: Runner = _run
           ) -> dict[str, list[str]]:  # fmt: skip
    files = listing(remote, run)
    todo = plan(files, local)
    if todo["new"] or todo["changed"]:
        local.mkdir(parents=True, exist_ok=True)
        res = run(["copy", remote, str(local), "--include", "*.pdf", "--include", "*.PDF",
                   "--max-size", MAX_SIZE])  # fmt: skip
        if res.returncode != 0:
            raise DriveError(f"rclone copy failed: {res.stderr.strip()[:300]}")
    if prune:
        for rel in todo["gone"]:
            (local / rel).unlink(missing_ok=True)
    return todo
