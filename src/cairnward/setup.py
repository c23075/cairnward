"""`cairnward setup`: choose a privacy preset, check prerequisites, next steps (spec §5.0)."""

from __future__ import annotations

import re
from collections.abc import Callable
from pathlib import Path

from cairnward.config.paths import Paths

PRESETS = {
    "open": "no protections in the way; audit log only. Nothing extra to install.",
    "balanced": "sandbox for agents, ask before new hosts and hosted models, vault on. "
    "Needs bubblewrap, socat, ripgrep and Node.js 22.12+.",
    "strict": "only listed hosts, no hosted models, tiered approvals, redaction enforced. "
    "Same requirements as balanced, plus a private git repo for audit anchors (M2).",
}

TEMPLATE = """\
#:schema https://github.com/c23075/cairnward/schemas/cairnward.schema.json
schema_version = 1

[privacy]
preset = "{preset}"
"""


def set_preset(main_config: Path, preset: str) -> None:
    """Write or update `[privacy] preset` without disturbing the rest of the file."""
    if preset not in PRESETS:
        raise ValueError(f"unknown preset '{preset}'")
    if not main_config.exists():
        main_config.parent.mkdir(parents=True, exist_ok=True)
        main_config.write_text(TEMPLATE.format(preset=preset), encoding="utf-8")
        return
    lines = main_config.read_text(encoding="utf-8").splitlines()
    start = next((i for i, x in enumerate(lines) if x.strip() == "[privacy]"), None)
    if start is None:
        lines += ["", "[privacy]", f'preset = "{preset}"']
    else:
        end = next(
            (i for i in range(start + 1, len(lines)) if lines[i].lstrip().startswith("[")),
            len(lines),
        )
        for i in range(start + 1, end):
            if re.match(r"\s*preset\s*=", lines[i]):
                lines[i] = f'preset = "{preset}"'
                break
        else:
            lines.insert(start + 1, f'preset = "{preset}"')
    main_config.write_text("\n".join(lines) + "\n", encoding="utf-8")


def choose(ask: Callable[[str], str], say: Callable[[str], None]) -> str:
    say("Choose a privacy preset (change it any time in cairnward.toml):")
    names = list(PRESETS)
    for i, name in enumerate(names, 1):
        say(f"  {i}. {name:9} {PRESETS[name]}")
    while True:
        reply = ask("Preset [1-3, default 2 = balanced]: ").strip().lower()
        if not reply:
            return "balanced"
        if reply.isdigit() and 1 <= int(reply) <= 3:
            return names[int(reply) - 1]
        if reply in PRESETS:
            return reply
        say("Please answer 1, 2, 3 or a preset name.")


def setup(
    paths: Paths,
    *,
    preset: str | None,
    ask: Callable[[str], str] | None,
    say: Callable[[str], None],
) -> str:
    if preset is None:
        if ask is None:
            raise ValueError("no terminal: pass --preset open|balanced|strict")
        preset = choose(ask, say)
    set_preset(paths.main_config, preset)
    say(f"Saved preset '{preset}' in {paths.main_config}")
    say("If you turn on backups: `cairnward backup init` creates a password on this disk. Keep a "
        "copy somewhere else, or losing this disk also makes the backups unreadable.")  # fmt: skip
    return preset
