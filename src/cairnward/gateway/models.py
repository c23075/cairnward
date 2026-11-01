"""Virtual models: `cw/<environment>:<effort>` picks environment and effort in any client
(spec §4.1, D48). `cw/raw/<model>` sends to a specific model with the default effort.
"""

from __future__ import annotations

from dataclasses import dataclass

from cairnward.backends.base import GenerationOptions
from cairnward.config.loader import Config

PREFIX = "cw/"


class UnknownModel(Exception):
    pass


@dataclass(frozen=True)
class Resolved:
    name: str  # what the client asked for
    environment: str
    effort: str
    backend: str
    model: str
    options: GenerationOptions


def virtual_model_ids(cfg: Config) -> list[str]:
    efforts = cfg.effort_levels_ordered()
    return [f"{PREFIX}{env}:{eff}" for env in sorted(cfg.environments) for eff in efforts]


def resolve(cfg: Config, name: str, *, default_environment: str | None = None) -> Resolved:
    if not name.startswith(PREFIX):
        raise UnknownModel(f"unknown model '{name}': use a cw/<environment>:<effort> model")
    rest = name[len(PREFIX) :]
    raw_model: str | None = None
    if rest.startswith("raw/"):
        raw_model = rest[4:]
        if not raw_model:
            raise UnknownModel("cw/raw/ needs a model name, e.g. cw/raw/qwen3:30b")
        env_name = default_environment or cfg.main.defaults.environment
        effort_name = None
    else:
        env_name, _, effort_name = rest.partition(":")
    env = cfg.environments.get(env_name)
    if env is None:
        raise UnknownModel(f"unknown environment '{env_name}' in model '{name}'")
    effort_name = effort_name or env.effort_default or cfg.main.defaults.effort
    level = cfg.effort.get(effort_name)
    if level is None:
        raise UnknownModel(f"unknown effort level '{effort_name}' in model '{name}'")

    role = level.model_role or "main"
    role_cfg = cfg.backends.roles.get(role) or cfg.backends.roles.get("main")
    if role_cfg is None:
        raise UnknownModel(f"no backend configured for the '{role}' role")
    model = raw_model or level.model or env.models.get(role) or role_cfg.model
    backend = level.backend or env.backend or role_cfg.backend
    options = GenerationOptions(
        thinking=level.thinking,
        context_tokens=level.context_tokens or env.context_tokens,
        max_tokens=level.max_tokens_per_request,
        temperature=level.temperature,
        top_p=level.top_p,
        top_k=level.top_k,
        min_p=level.min_p,
    )
    return Resolved(name, env_name, effort_name, backend, model, options)
