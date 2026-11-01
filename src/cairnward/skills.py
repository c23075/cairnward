"""Skills: SKILL.md folders served to clients through the MCP endpoint (spec §4.2, D15,
FR-MCP-4).

A skill is a folder under `<config>/skills/<name>/` with a `SKILL.md` whose front matter
gives a name and a one-line description, followed by the instructions; other files in
the folder (references, templates, scripts) come along as read-only resources. An
environment offers the skills listed in its `skills = [...]`.

Clients see each skill as an MCP prompt (`prompts/list`, `prompts/get`) and its files as
resources (`skill://<name>/<path>`). Cairnward never runs a skill's scripts.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from pathlib import Path

MAX_FILE = 1024 * 1024
_NAME = re.compile(r"^[a-z0-9][a-z0-9_-]{0,62}$")


@dataclass(frozen=True)
class Skill:
    name: str
    description: str
    folder: Path
    body: str


class SkillError(Exception):
    pass


def _front_matter(text: str) -> tuple[dict[str, str], str]:
    if not text.startswith("---"):
        return {}, text
    end = text.find("\n---", 3)
    if end == -1:
        return {}, text
    meta = {}
    for line in text[3:end].splitlines():
        key, sep, value = line.partition(":")
        if sep and key.strip():
            meta[key.strip().lower()] = value.strip().strip("\"'")
    return meta, text[end + 4 :].lstrip("\n")


def load(skills_dir: Path) -> dict[str, Skill]:
    found: dict[str, Skill] = {}
    if not skills_dir.is_dir():
        return found
    for folder in sorted(p for p in skills_dir.iterdir() if p.is_dir()):
        md = folder / "SKILL.md"
        if not md.is_file() or not _NAME.match(folder.name):
            continue
        meta, body = _front_matter(md.read_text(encoding="utf-8", errors="replace"))
        found[folder.name] = Skill(folder.name, meta.get("description", "")[:500], folder, body)
    return found


def files(skill: Skill) -> list[str]:
    """Readable files of the skill (relative paths), excluding SKILL.md itself."""
    root = skill.folder.resolve()
    out = []
    for p in sorted(skill.folder.rglob("*")):
        rel = p.relative_to(skill.folder)
        if any(part.startswith(".") for part in rel.parts) or not p.is_file():
            continue
        if rel.as_posix() == "SKILL.md" or not p.resolve().is_relative_to(root):
            continue  # symlinks out of the folder are not served
        if p.stat().st_size <= MAX_FILE:
            out.append(rel.as_posix())
    return out


def read(skill: Skill, rel: str) -> str:
    if rel not in files(skill):
        raise SkillError(f"no file '{rel}' in skill '{skill.name}'")
    data = (skill.folder / rel).read_bytes()
    try:
        return data.decode("utf-8")
    except UnicodeDecodeError:
        raise SkillError(f"'{rel}' isn't a text file") from None
