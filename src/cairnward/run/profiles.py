"""How each coding agent is pointed at Cairnward (spec §4.3, FR-AGT-1)."""

from __future__ import annotations

import json
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path

# (base_url, key, model, fast_model, run_dir) -> extra environment; may write config files
# into run_dir, which exists only for the run and is removed afterwards.
ConfigWriter = Callable[[str, str, str, str, Path], dict[str, str]]


@dataclass(frozen=True)
class Profile:
    name: str
    tested: bool
    env: Callable[[str, str, str, str], dict[str, str]]  # (base_url, key, model, fast_model)
    writable_home_dirs: tuple[str, ...] = ()
    notes: str = ""
    extra_hosts: list[str] = field(default_factory=list)
    config: ConfigWriter | None = None


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
        # Small edits instead of whole files: far fewer tokens to write on a CPU (D64).
        "AIDER_EDIT_FORMAT": "diff",
        # litellm otherwise downloads a model price list at start-up.
        "LITELLM_LOCAL_MODEL_COST_MAP": "True",
    }


def _qwen_code(base: str, key: str, model: str, fast: str) -> dict[str, str]:
    return {**_generic(base, key, model, fast), "OPENAI_MODEL": model}


def _opencode_config(base: str, key: str, model: str, fast: str, run_dir: Path) -> dict[str, str]:
    cfg = {
        "$schema": "https://opencode.ai/config.json",
        "provider": {"cairnward": {
            "npm": "@ai-sdk/openai-compatible",
            "name": "Cairnward",
            "options": {"baseURL": base, "apiKey": key},
            "models": {model: {"name": model}, fast: {"name": fast}},
        }},
        "model": f"cairnward/{model}",
        "small_model": f"cairnward/{fast}",
        "autoupdate": False,
        "share": "disabled",
    }  # fmt: skip
    path = run_dir / "opencode.json"
    path.write_text(json.dumps(cfg, indent=2), encoding="utf-8")
    path.chmod(0o600)
    return {"OPENCODE_CONFIG": str(path), "OPENCODE_DISABLE_AUTOUPDATE": "1",
            "OPENCODE_DISABLE_MODELS_FETCH": "1"}  # fmt: skip


def _goose(base: str, key: str, model: str, fast: str) -> dict[str, str]:
    host = base.removesuffix("/v1")
    return {**_generic(base, key, model, fast), "GOOSE_PROVIDER": "openai", "OPENAI_HOST": host,
            "OPENAI_BASE_PATH": "v1/chat/completions", "GOOSE_MODEL": model,
            "GOOSE_DISABLE_KEYRING": "1"}  # fmt: skip


PROFILES: dict[str, Profile] = {
    "aider": Profile(
        "aider",
        True,
        _aider,
        (".aider", ".cache"),
        "lean prompts and diff edits: the best fit for CPU-only machines (D64)",
    ),
    "qwen-code": Profile(
        "qwen-code",
        True,
        _qwen_code,
        (".qwen", ".cache"),
        "large system prompt (slow first turn on CPU); tries to send usage telemetry "
        "(blocked in the sandbox)",
    ),
    "opencode": Profile(
        "opencode",
        True,
        _generic,
        (".local/share/opencode", ".config/opencode", ".cache/opencode"),
        "configured through a per-run config file; tries registry.npmjs.org at start "
        "(blocked in the sandbox) and works without it; in scripts give it stdin </dev/null",
        config=_opencode_config,
    ),
    "goose": Profile(
        "goose",
        False,
        _goose,
        (".config/goose", ".local/share/goose", ".local/state/goose"),
        "untested: settings follow Goose's documented OpenAI-compatible provider variables",
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
