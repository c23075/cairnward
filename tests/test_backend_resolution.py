"""Model/backend resolution order (FR-BACK-2): request > effort level > client >
environment > global roles."""

from pathlib import Path

import pytest

from cairnward.config import load_config
from cairnward.config.loader import ConfigError
from cairnward.config.paths import Paths
from cairnward.gateway.models import resolve
from tests.test_gateway import BACKENDS


def cfg_with(tmp_path: Path, *, clients: str = "", env: str = "", effort: str = "") -> object:
    paths = Paths(tmp_path / "cfg", tmp_path / "data", tmp_path / "state")
    (paths.config_dir / "environments").mkdir(parents=True, exist_ok=True)
    (paths.config_dir / "backends.toml").write_text(BACKENDS)
    (paths.config_dir / "clients.toml").write_text(clients)
    (paths.config_dir / "environments" / "e.toml").write_text(env)
    (paths.config_dir / "effort.toml").write_text(effort)
    return load_config(paths)


def test_order(tmp_path: Path) -> None:
    cfg = cfg_with(tmp_path)
    r = resolve(cfg, "cw/e:medium")  # type: ignore[arg-type]
    assert (r.backend, r.model) == ("local-ollama", "qwen3:30b")  # global role

    cfg = cfg_with(tmp_path, env='[models]\nmain = "env-model"\n')
    assert resolve(cfg, "cw/e:medium").model == "env-model"  # type: ignore[arg-type]

    cfg = cfg_with(tmp_path, env='[models]\nmain = "env-model"\n', clients=(
        '[clients.jan]\nkind = "chat"\nprofile = "jan"\nbackend = "llama"\n'
        '[clients.jan.models]\nmain = "client-model"\n'))  # fmt: skip
    r = resolve(cfg, "cw/e:medium", client="jan")  # type: ignore[arg-type]
    assert (r.backend, r.model) == ("llama", "client-model")  # client beats environment
    assert resolve(cfg, "cw/e:medium", client="other").model == "env-model"  # type: ignore[arg-type]
    assert resolve(cfg, "cw/raw/exact:1b", default_environment="e", client="jan").model == (  # type: ignore[arg-type]
        "exact:1b")  # the request itself wins  # fmt: skip


def test_fallback_must_be_local(tmp_path: Path) -> None:
    hosted_fallback = BACKENDS.replace(
        '[backends.llama]\ndriver = "openai_compat"\n',
        '[backends.llama]\ndriver = "openai_compat"\nfallback = "cloud"\n',
    )
    paths = Paths(tmp_path / "cfg", tmp_path / "data", tmp_path / "state")
    paths.config_dir.mkdir(parents=True)
    (paths.config_dir / "backends.toml").write_text(hosted_fallback)
    with pytest.raises(ConfigError, match="isn't local"):
        load_config(paths)
    (paths.config_dir / "backends.toml").write_text(hosted_fallback.replace('"cloud"', '"nope"'))
    with pytest.raises(ConfigError, match="unknown backend 'nope'"):
        load_config(paths)
