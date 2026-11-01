"""Load and validate all configuration files, reporting every problem at once."""

from __future__ import annotations

import tomllib
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from pydantic import BaseModel, ValidationError

from cairnward.config.models import (
    BackendsConfig,
    ClientsConfig,
    EffortConfig,
    EffortLevel,
    EnvironmentConfig,
    MainConfig,
    _check_name,
    builtin_environments,
    default_effort_levels,
)
from cairnward.config.paths import Paths, default_paths
from cairnward.config.privacy import EffectivePrivacy, resolve_privacy


class ConfigError(Exception):
    """One or more configuration problems; `problems` holds 'file: field: message' lines."""

    def __init__(self, problems: list[str]):
        self.problems = problems
        super().__init__("\n".join(problems))


@dataclass
class Config:
    paths: Paths
    main: MainConfig
    backends: BackendsConfig
    clients: ClientsConfig
    effort: dict[str, EffortLevel]
    environments: dict[str, EnvironmentConfig]
    files_loaded: list[Path] = field(default_factory=list)

    def privacy(self, environment: str | None = None) -> EffectivePrivacy:
        """Effective privacy settings globally, or for one environment."""
        if environment is None:
            return resolve_privacy(self.main.privacy)
        env = self.environments[environment]
        return resolve_privacy(self.main.privacy, env.privacy, environment)

    def effort_levels_ordered(self) -> list[str]:
        visible = [(lvl.order, name) for name, lvl in self.effort.items() if not lvl.hidden]
        return [name for _, name in sorted(visible)]


def _read_toml(path: Path, problems: list[str]) -> dict[str, Any] | None:
    if not path.exists():
        return None
    try:
        with path.open("rb") as fh:
            return tomllib.load(fh)
    except tomllib.TOMLDecodeError as exc:
        problems.append(f"{path}: not valid TOML: {exc}")
    except OSError as exc:
        problems.append(f"{path}: cannot read: {exc}")
    return None


def _validate[M: BaseModel](
    model: type[M], data: dict[str, Any], path: Path, problems: list[str]
) -> M | None:
    try:
        return model.model_validate(data)
    except ValidationError as exc:
        for err in exc.errors():
            loc = ".".join(str(p) for p in err["loc"]) or "(file)"
            msg = err["msg"].removeprefix("Value error, ")
            if err["type"] == "extra_forbidden":
                msg = "unknown setting (check the spelling)"
            problems.append(f"{path}: {loc}: {msg}")
        return None


def load_config(paths: Paths | None = None) -> Config:
    """Load every file under the config directory; missing files mean built-in defaults."""
    paths = paths or default_paths()
    problems: list[str] = []
    loaded: list[Path] = []

    def load[T: BaseModel](model: type[T], path: Path) -> T:
        data = _read_toml(path, problems)
        if data is None:
            return model()
        loaded.append(path)
        return _validate(model, data, path, problems) or model()

    main = load(MainConfig, paths.main_config)
    backends = load(BackendsConfig, paths.backends_config)
    clients = load(ClientsConfig, paths.clients_config)
    effort_file = load(EffortConfig, paths.effort_config)

    effort = default_effort_levels()
    effort.update(effort_file.effort)

    environments = builtin_environments()
    if paths.environments_dir.is_dir():
        for env_path in sorted(paths.environments_dir.glob("*.toml")):
            name = env_path.stem
            try:
                _check_name(name, "environment file name")
            except ValueError as exc:
                problems.append(f"{env_path}: {exc}")
                continue
            data = _read_toml(env_path, problems)
            if data is None:
                continue
            loaded.append(env_path)
            env = _validate(EnvironmentConfig, data, env_path, problems)
            if env is not None:
                environments[name] = env

    cfg = Config(paths, main, backends, clients, effort, environments, loaded)
    problems.extend(_cross_check(cfg))
    if problems:
        raise ConfigError(problems)
    return cfg


def _cross_check(cfg: Config) -> list[str]:
    """References between files."""
    p = cfg.paths
    out: list[str] = []
    backends = cfg.backends.backends
    if cfg.main.defaults.environment not in cfg.environments:
        out.append(
            f"{p.main_config}: defaults.environment: unknown environment "
            f"'{cfg.main.defaults.environment}'"
        )
    if cfg.main.defaults.effort not in cfg.effort:
        out.append(
            f"{p.main_config}: defaults.effort: unknown effort level '{cfg.main.defaults.effort}'"
        )
    for name, lvl in cfg.effort.items():
        if lvl.backend and lvl.backend not in backends:
            out.append(f"{p.effort_config}: effort.{name}.backend: unknown backend '{lvl.backend}'")
        if lvl.subagents.backend and lvl.subagents.backend not in backends:
            out.append(
                f"{p.effort_config}: effort.{name}.subagents.backend: unknown backend "
                f"'{lvl.subagents.backend}'"
            )
        if lvl.subagents.effort and lvl.subagents.effort not in cfg.effort:
            out.append(
                f"{p.effort_config}: effort.{name}.subagents.effort: unknown effort level "
                f"'{lvl.subagents.effort}'"
            )
    for name, env in cfg.environments.items():
        src = p.environments_dir / f"{name}.toml"
        if env.backend and env.backend not in backends:
            out.append(f"{src}: backend: unknown backend '{env.backend}'")
        if env.effort_default and env.effort_default not in cfg.effort:
            out.append(f"{src}: effort_default: unknown effort level '{env.effort_default}'")
    for name, client in cfg.clients.clients.items():
        if client.default_environment and client.default_environment not in cfg.environments:
            out.append(
                f"{p.clients_config}: clients.{name}.default_environment: unknown environment "
                f"'{client.default_environment}'"
            )
    return out
