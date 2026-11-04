"""Where Cairnward keeps its files.

Linux/macOS follow the XDG base-directory convention; Windows uses %APPDATA% and
%LOCALAPPDATA%. Every location can be overridden with an environment variable, which
the tests and `--config-dir` use.
"""

from __future__ import annotations

import os
import stat
import sys
from dataclasses import dataclass
from pathlib import Path

APP = "cairnward"


def _xdg(var: str, fallback: str) -> Path:
    value = os.environ.get(var)
    if value and Path(value).is_absolute():
        return Path(value)
    return Path.home() / fallback


@dataclass(frozen=True)
class Paths:
    config_dir: Path
    data_dir: Path
    state_dir: Path

    @property
    def main_config(self) -> Path:
        return self.config_dir / "cairnward.toml"

    @property
    def backends_config(self) -> Path:
        return self.config_dir / "backends.toml"

    @property
    def clients_config(self) -> Path:
        return self.config_dir / "clients.toml"

    @property
    def effort_config(self) -> Path:
        return self.config_dir / "effort.toml"

    @property
    def mcp_config(self) -> Path:
        return self.config_dir / "mcp.toml"

    @property
    def environments_dir(self) -> Path:
        return self.config_dir / "environments"


def private_by_default() -> None:
    """New files are owner-only in this process and what it starts (SQLite's included)."""
    if os.name != "nt":
        os.umask(0o077)


def wider_than_owner(paths: Paths) -> list[Path]:
    """Files and folders in Cairnward's folders that others could read."""
    out = []
    for root in (paths.config_dir, paths.data_dir, paths.state_dir):
        if not root.is_dir() or os.name == "nt":
            continue
        for folder, dirs, files in os.walk(root):
            for entry in [Path(folder), *(Path(folder) / n for n in [*dirs, *files])]:
                mode = entry.lstat().st_mode
                if mode & 0o077 and (stat.S_ISREG(mode) or stat.S_ISDIR(mode)):
                    out.append(entry)
    return sorted(set(out))


def make_owner_only(paths: Paths) -> int:
    """Remove others' access from files made before `private_by_default`; returns how many.
    Owner access is kept as it was, so programs in the engines' folders still run."""
    wide = wider_than_owner(paths)
    for path in wide:
        path.chmod(stat.S_IMODE(os.lstat(path).st_mode) & 0o700)
    return len(wide)


def default_paths(config_dir: Path | None = None) -> Paths:
    """Resolve paths. Precedence: argument > CAIRNWARD_*_DIR env vars > platform default."""
    if sys.platform == "win32":
        appdata = Path(os.environ.get("APPDATA", Path.home() / "AppData" / "Roaming"))
        local = Path(os.environ.get("LOCALAPPDATA", Path.home() / "AppData" / "Local"))
        cfg, data, state = appdata / APP, local / APP / "data", local / APP / "state"
    else:
        cfg = _xdg("XDG_CONFIG_HOME", ".config") / APP
        data = _xdg("XDG_DATA_HOME", ".local/share") / APP
        state = _xdg("XDG_STATE_HOME", ".local/state") / APP
    env_cfg = os.environ.get("CAIRNWARD_CONFIG_DIR")
    env_data = os.environ.get("CAIRNWARD_DATA_DIR")
    env_state = os.environ.get("CAIRNWARD_STATE_DIR")
    return Paths(
        config_dir=config_dir or (Path(env_cfg) if env_cfg else cfg),
        data_dir=Path(env_data) if env_data else data,
        state_dir=Path(env_state) if env_state else state,
    )
