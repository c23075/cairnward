"""Command-line entry point.

Only `--version` exists so far; subcommands (`serve`, `status`, `doctor`, `audit`, ...)
arrive with the Phase 0 tasks in docs/plan/phase0.md.
"""

from __future__ import annotations

import argparse
from collections.abc import Sequence

from cairn import __version__


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="cairn",
        description="Local, privacy-first AI agent for coding and literature review.",
    )
    parser.add_argument("--version", action="version", version=f"cairn {__version__}")
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    parser = build_parser()
    parser.parse_args(argv)
    parser.print_help()
    return 0
