#!/usr/bin/env python3
"""NFR-MAINT-1: at least 80% coverage in every package, not just on average, so a strong
package can't hide a weak one. Reads `coverage json`; exits 1 and names what's short.

    uv run coverage json -o coverage.json && python scripts/check_coverage.py coverage.json

Only what the test job can't measure is left out, each for a stated reason. Anything new is
counted unless it's added here.
"""

from __future__ import annotations

import json
import sys
from collections import defaultdict
from pathlib import Path
from typing import Any

MINIMUM = 80.0
PREFIX = "src/cairnward/"
LEFT_OUT = {
    "__main__.py": "two lines that call cli.main",
    "cli.py": "argument handling for the logic counted elsewhere; much of it needs a live server",
    "check.py": "a report about the machine: needs real models",
    "doctor.py": "a report about the machine: many checks need the real sandbox and tools",
    "core/server.py": "runs as a subprocess this job can't measure; tests/isolation runs it",
    "redaction_engines/presidio_worker.py": "a worker with its own Python; a real run tests it",
    "plugins/cli.py": "argument handling; tests/isolation runs it for real",
    "plugins/conformance.py": "needs the real sandbox; tests/isolation runs it",
}


def package_of(name: str) -> str:
    return name.split("/", 1)[0] if "/" in name else name


def percent(counts: list[int]) -> float:
    covered, total = counts[0] + counts[2], counts[1] + counts[3]
    return 100.0 * covered / total if total else 100.0


def by_package(report: dict[str, Any]) -> dict[str, list[int]]:
    """Package name -> [covered lines, lines, covered branches, branches]."""
    packages: dict[str, list[int]] = defaultdict(lambda: [0, 0, 0, 0])
    for path, data in report["files"].items():
        name = path.replace("\\", "/").split(PREFIX, 1)[-1]
        if name.startswith(tuple(LEFT_OUT)):
            continue
        s, counts = data["summary"], packages[package_of(name)]
        counts[0] += s["covered_lines"]
        counts[1] += s["num_statements"]
        counts[2] += s.get("covered_branches", 0)
        counts[3] += s.get("num_branches", 0)
    return dict(packages)


def main(argv: list[str]) -> int:
    packages = by_package(json.loads(Path(argv[1]).read_text(encoding="utf-8")))
    if not packages:
        print("no packages found: is this the right coverage file?")
        return 1
    total = [sum(c[i] for c in packages.values()) for i in range(4)]
    short = {name: percent(c) for name, c in packages.items() if percent(c) < MINIMUM}
    for name, counts in sorted(packages.items(), key=lambda kv: percent(kv[1])):
        print(f"{'SHORT' if name in short else 'ok':6}{name:20}{percent(counts):6.1f}%")
    print(f"counted code: {percent(total):.1f}% over {total[1]} statements; minimum per package "
          f"{MINIMUM:g}%")  # fmt: skip
    return 1 if short else 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
