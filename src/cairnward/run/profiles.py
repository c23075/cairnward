"""How each coding agent is pointed at Cairnward (spec §4.3, FR-AGT-1)."""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path


@dataclass(frozen=True)
class Profile:
    name: str
    tested: bool
    env: Callable[[str, str, str, str], dict[str, str]]  # (base_url, key, model, fast_model)
    writable_home_dirs: tuple[str, ...] = ()
    notes: str = ""
    extra_hosts: list[str] = field(default_factory=list)


def _generic(base: str, key: str, model: str, fast: str) -> dict[str, str]:
    return {
        "OPENAI_BASE_URL": base,
        "OPENAI_API_BASE": base,
        "OPENAI_API_KEY": key,
        "CAIRNWARD_MODEL": model,
        "CAIRNWARD_FAST_MODEL": fast,
    }


def _aider(base: str, key: str, model: str, fast: str) -> dict[str, str]:
    return {
        **_generic(base, key, model, fast),
        "AIDER_MODEL": f"openai/{model}",
        "AIDER_WEAK_MODEL": f"openai/{fast}",
        "AIDER_ANALYTICS": "false",
        "AIDER_CHECK_UPDATE": "false",
        "AIDER_SHOW_RELEASE_NOTES": "false",
        # litellm otherwise downloads a model price list at start-up.
        "LITELLM_LOCAL_MODEL_COST_MAP": "True",
    }


def _qwen_code(base: str, key: str, model: str, fast: str) -> dict[str, str]:
    return {**_generic(base, key, model, fast), "OPENAI_MODEL": model}


PROFILES: dict[str, Profile] = {
    "aider": Profile(
        "aider",
        True,
        _aider,
        (".aider", ".cache"),
        "lean prompts; the best fit for CPU-only machines (D64)",
    ),
    "qwen-code": Profile(
        "qwen-code",
        False,
        _qwen_code,
        (".qwen", ".cache"),
        "large system prompt: slow first turn on CPU",
    ),
    "generic": Profile(
        "generic",
        True,
        _generic,
        (".cache",),
        "any OpenAI-compatible agent: reads OPENAI_BASE_URL / OPENAI_API_KEY",
    ),
}


def home_dirs(profile: Profile, home: Path | None = None) -> list[Path]:
    home = home or Path.home()
    return [home / d for d in profile.writable_home_dirs]
