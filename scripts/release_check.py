#!/usr/bin/env python3
"""Checks for a release, run by .github/workflows/release.yml and usable by hand.

    release_check.py version <tag>       the tag must be v + the version in pyproject.toml
    release_check.py wheel <file>        the wheel holds what Cairnward needs at run time
    release_check.py sbom <file>         the SBOM lists the runtime dependencies, not dev tools

Uses only the standard library. Exits 1 and says why when a check fails.
"""

from __future__ import annotations

import json
import sys
import tomllib
import zipfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
NEEDED_IN_WHEEL = (
    "cairnward/dashboard/static/index.html",
    "cairnward/research/csl/MANIFEST.json",
    "cairnward/research/engines/paperqa-requirements.txt",
)
NEVER_IN_SBOM = {"pytest", "ruff", "mypy"}  # dev tools mean the wrong dependency group


def project() -> dict[str, object]:
    data: dict[str, object] = tomllib.loads((ROOT / "pyproject.toml").read_text("utf-8"))["project"]
    return data


def check_version(tag: str) -> str | None:
    want = f"v{project()['version']}"
    return None if tag == want else f"tag {tag} doesn't match pyproject.toml's version ({want})"


def check_wheel(path: Path) -> str | None:
    names = set(zipfile.ZipFile(path).namelist())
    missing = [n for n in NEEDED_IN_WHEEL if n not in names]
    return f"{path.name} is missing {', '.join(missing)}" if missing else None


def check_sbom(path: Path) -> str | None:
    data = json.loads(path.read_text("utf-8"))
    if data.get("bomFormat") != "CycloneDX":
        return f"{path.name} isn't a CycloneDX document"
    found = {str(c.get("name", "")).lower() for c in data.get("components", [])}
    dev = sorted(found & NEVER_IN_SBOM)
    if dev:
        return f"{path.name} lists development tools: {', '.join(dev)}"
    deps = project()["dependencies"]
    if not isinstance(deps, list):
        return "pyproject.toml has no dependency list"
    wanted = {str(d).split(">")[0].split("=")[0].split("<")[0].split("[")[0].strip().lower()
              for d in deps}  # fmt: skip
    missing = sorted(wanted - found)
    return f"{path.name} doesn't list {', '.join(missing)}" if missing else None


def main(argv: list[str]) -> int:
    checks = {"version": lambda v: check_version(v), "wheel": lambda v: check_wheel(Path(v)),
              "sbom": lambda v: check_sbom(Path(v))}  # fmt: skip
    if len(argv) != 2 or argv[0] not in checks:
        print(__doc__, file=sys.stderr)
        return 2
    problem = checks[argv[0]](argv[1])
    if problem:
        print(f"release check failed: {problem}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
