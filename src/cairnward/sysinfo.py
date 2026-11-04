"""Small system facts (Linux /proc; empty elsewhere)."""

from __future__ import annotations

from pathlib import Path

GB = 1024**3
PROC = Path("/proc")
WINDOWS_FILESYSTEMS = {"9p", "drvfs", "virtiofs"}  # how WSL mounts Windows folders
INTEROP_FILES = ("WSLInterop", "WSLInterop-late")


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


def wsl_version(proc: Path = PROC) -> int | None:
    """1 or 2 inside Windows Subsystem for Linux, else None. Only WSL1's kernel name ends in
    "Microsoft"; a custom WSL2 kernel may not mention it, but still registers interop."""
    try:
        release = (proc / "sys/kernel/osrelease").read_text().strip()
    except OSError:
        return None
    binfmt = proc / "sys/fs/binfmt_misc"
    if "microsoft" in release.lower() or any((binfmt / f).exists() for f in INTEROP_FILES):
        return 1 if release.endswith("Microsoft") else 2
    return None


def windows_mounts(proc: Path = PROC) -> list[Path]:
    """Where Windows folders are mounted (/mnt/c, or wherever wsl.conf's automount puts them)."""
    try:
        lines = (proc / "mounts").read_text().splitlines()
    except OSError:
        return []
    out = []
    for line in lines:
        fields = line.split()
        if len(fields) > 2 and fields[2] in WINDOWS_FILESYSTEMS:
            out.append(Path(fields[1].replace("\\040", " ")))
    return out


def on_windows_drive(path: Path, proc: Path = PROC) -> bool:
    if wsl_version(proc) is None:
        return False
    real = path.resolve()  # a link in the Linux file system can point at a drive
    return any(real == m or m in real.parents for m in windows_mounts(proc))


def wsl_interop_on(proc: Path = PROC) -> bool:
    """Whether Linux programs here can start Windows programs."""
    for name in INTEROP_FILES:
        try:
            text = (proc / "sys/fs/binfmt_misc" / name).read_text()
        except OSError:
            continue
        if text.startswith("enabled"):
            return True
    return False
