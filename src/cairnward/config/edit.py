"""Change single privacy settings from the dashboard (or any caller with the owner's
authority), in the same TOML files a person edits.

Only the enumerated settings and the preset can be changed this way (no allow-lists,
no backends). A change is written only if the whole configuration still loads; one that
leaves a changed setting weaker than Strict needs `confirm`. Changes take
effect when Cairnward restarts, like an edit by hand; the caller records them in the
audit log.
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Any, get_args

from cairnward.config.loader import Config, ConfigError, load_config
from cairnward.config.privacy import SETTINGS, PrivacyOverrides


class EditError(Exception):
    pass


def options() -> dict[str, list[str]]:
    """Each editable setting with its allowed values ("preset" included)."""
    out: dict[str, list[str]] = {}
    for name in ["preset", *SETTINGS]:
        ann = PrivacyOverrides.model_fields[name].annotation
        values = [a for arg in get_args(ann) for a in (get_args(arg) or (arg,))]
        out[name] = [v for v in values if isinstance(v, str)]
    return out


@dataclass
class Change:
    scope: str  # "global" or an environment name
    setting: str
    before: str
    after: str


def _toml(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False)  # strings, lists, numbers, booleans


def set_key(path: Path, table: str, key: str, value: str | int | None) -> None:
    """Set (or with None remove) `key` in `[table]` of a TOML file, keeping everything else."""
    lines = path.read_text(encoding="utf-8").splitlines() if path.exists() else []
    start = next((i for i, x in enumerate(lines) if x.strip() == f"[{table}]"), None)
    if start is None:
        if value is not None:
            lines += ["", f"[{table}]", f"{key} = {_toml(value)}"]
    else:
        end = next((i for i in range(start + 1, len(lines))
                    if lines[i].lstrip().startswith("[")), len(lines))  # fmt: skip
        found = next((i for i in range(start + 1, end)
                      if re.match(rf"\s*{re.escape(key)}\s*=", lines[i])), None)  # fmt: skip
        if found is not None and value is None:
            del lines[found]
        elif found is not None:
            lines[found] = f"{key} = {_toml(value)}"
        elif value is not None:
            lines.insert(start + 1, f"{key} = {_toml(value)}")
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")


def change_backup(cfg: Config, mode: str, repository: str | None,
                  interval_hours: int | None = None) -> tuple[Config, list[str]]:  # fmt: skip
    """Set the backup mode and folder; returns the new configuration and what changed.
    Only a folder can be set here: a remote repository names a bucket and may need
    credentials, so it stays a change made in the file, and an existing one is left alone."""
    from cairnward import backup

    b = cfg.main.backup
    if mode not in ("none", "on_demand", "auto"):
        raise EditError("mode must be none, on_demand or auto")
    repository = (repository or "").strip() or None
    remote = backup.remote_kind(cfg) is not None
    if repository not in (None, b.repository) and not repository.startswith(("/", "~")):
        raise EditError("use a full path to a folder, like /mnt/backup/cairnward (a remote "
                        "repository is set in cairnward.toml)")  # fmt: skip
    if remote and repository is None:
        repository = b.repository
    if mode != "none" and not repository:
        raise EditError(f"{mode} needs a folder to back up to")
    path = cfg.paths.main_config
    before = path.read_text(encoding="utf-8") if path.exists() else None
    wanted: dict[str, str | int | None] = {"mode": mode, "repository": repository}
    if interval_hours is not None:
        wanted["interval_hours"] = interval_hours

    def undo() -> None:
        if before is None:
            path.unlink(missing_ok=True)
        else:
            path.write_text(before, encoding="utf-8")

    try:
        for key, value in wanted.items():
            set_key(path, "backup", key, value)
        new = load_config(cfg.paths)
    except ConfigError as exc:
        undo()
        raise EditError(f"not saved, the configuration wouldn't load: {exc}") from None
    except OSError as exc:
        undo()
        raise EditError(f"can't write {path.name}: {exc.strerror or exc} (edit the file by "
                        "hand, or use the CLI outside the sandbox)") from None  # fmt: skip
    return new, [k for k, v in wanted.items() if getattr(b, k) != v]


def _environment_file(cfg: Config, name: str) -> Path:
    """The environment's file; a built-in one is first written out with its settings,
    since a file of the same name replaces the built-in."""
    path = cfg.paths.environments_dir / f"{name}.toml"
    if path.exists():
        return path
    from cairnward.config.models import builtin_environments

    base = builtin_environments().get(name)
    if base is None:
        raise EditError(f"unknown environment '{name}'")
    data = base.model_dump(mode="json", exclude_defaults=True)
    privacy = data.pop("privacy", {}) or {}
    lines = [f"# written by Cairnward from the built-in '{name}' environment"]
    lines += [f"{k} = {_toml(v)}" for k, v in data.items()]
    if privacy:
        lines += ["", "[privacy]"] + [f"{k} = {_toml(v)}" for k, v in privacy.items()]
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    return path


def _effective(cfg: Config) -> dict[str, dict[str, tuple[str, bool]]]:
    """scope -> setting -> (value, weaker than Strict)."""
    scopes: dict[str, Any] = {"global": cfg.privacy()}
    scopes.update({n: cfg.privacy(n) for n in cfg.environments})
    return {s: {k: (v.value, v.weaker_than_strict) for k, v in p.settings.items()}
            for s, p in scopes.items()}  # fmt: skip


def change_privacy(cfg: Config, scope: str, setting: str, value: str | None, *,
                   confirm: bool = False) -> tuple[Config, list[Change]]:  # fmt: skip
    """Apply one change; returns the new configuration and every effective change it
    makes (a global change shows in each environment that doesn't override it).
    Raises EditError (nothing written) for a bad value, a configuration that wouldn't
    load, or a weakening without `confirm` (the message lists what would weaken)."""
    allowed = options()
    if setting not in allowed:
        raise EditError(f"'{setting}' can't be changed here (edit the configuration files)")
    if value is not None and value not in allowed[setting]:
        raise EditError(f"{setting} must be one of: {', '.join(allowed[setting])}")
    if scope == "global":
        path, table = cfg.paths.main_config, "privacy"
    elif scope in cfg.environments:
        path, table = cfg.paths.environments_dir / f"{scope}.toml", "privacy"
    else:
        raise EditError(f"unknown environment '{scope}'")
    if scope == "global" and setting == "preset" and value is None:
        raise EditError("the global preset can't be removed; choose one")
    before_files = {p: p.read_text(encoding="utf-8") if p.exists() else None
                    for p in (cfg.paths.main_config, path)}  # fmt: skip

    def undo() -> None:
        for p, text in before_files.items():
            if text is None:
                p.unlink(missing_ok=True)
            else:
                p.write_text(text, encoding="utf-8")

    try:
        if scope != "global":
            path = _environment_file(cfg, scope)
        set_key(path, table, setting, value)
        new = load_config(cfg.paths)
    except ConfigError as exc:
        undo()
        raise EditError(f"not saved, the configuration wouldn't load: {exc}") from None
    except OSError as exc:
        undo()
        raise EditError(f"can't write {path.name}: {exc.strerror or exc} (edit the file by "
                        "hand, or use the CLI outside the sandbox)") from None  # fmt: skip
    old, now = _effective(cfg), _effective(new)
    changed = [(s, k) for s in now if s in old for k in now[s] if now[s][k][0] != old[s][k][0]]
    changes = [Change(s, k, old[s][k][0], now[s][k][0]) for s, k in changed]
    # weaker than Strict afterwards (e.g. network ask -> open; off -> allowlist isn't)
    weaker = [c for c in changes if now[c.scope][c.setting][1]]
    if weaker and not confirm:
        undo()
        raise EditError("needs confirmation: weaker than Strict afterwards: " + ", ".join(
            f"{c.setting} = {c.after} ({c.scope})" for c in weaker[:8]))  # fmt: skip
    return new, changes
