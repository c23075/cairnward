from pathlib import Path

import pytest

from cairnward.config import ConfigError, load_config
from cairnward.config.models import Backend, ServerConfig, parse_duration
from cairnward.config.paths import Paths


@pytest.fixture
def paths(tmp_path: Path) -> Paths:
    return Paths(config_dir=tmp_path / "cfg", data_dir=tmp_path / "data", state_dir=tmp_path / "st")


def write(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")


def test_no_files_gives_spec_defaults(paths: Paths) -> None:
    cfg = load_config(paths)
    assert cfg.files_loaded == []
    assert cfg.backends.roles["main"].model == "qwen3:30b"
    assert cfg.backends.roles["fast"].model == "qwen3:1.7b"
    assert cfg.effort_levels_ordered() == ["low", "medium", "high"]
    assert cfg.effort["low"].model_role == "fast"
    assert set(cfg.environments) == {"coding", "scratch"}
    assert cfg.environments["coding"].context_tokens == 8192
    assert cfg.privacy().label == "Balanced"
    assert cfg.privacy("scratch").label == "Open"


def test_full_example_loads(paths: Paths) -> None:
    c = paths.config_dir
    write(
        c / "cairnward.toml",
        """
schema_version = 1
[privacy]
preset = "strict"
approvals = "high_only"
[defaults]
environment = "client"
effort = "deep-review"
""",
    )
    write(
        c / "backends.toml",
        """
[backends.local-ollama]
driver = "ollama"
url = "http://127.0.0.1:11434"
[backends.hosted-qwen]
driver = "openai_compat"
url = "https://dashscope-intl.aliyuncs.com/compatible-mode/v1"
location = "hosted"
api_key = "keyring:hosted-qwen"
[roles.main]
backend = "local-ollama"
model = "qwen3:30b"
""",
    )
    write(
        c / "effort.toml",
        """
[effort.deep-review]
model = "qwen3:30b"
backend = "local-ollama"
thinking = true
max_tool_steps = 40
subagents = { max = 6, effort = "medium" }
wall_clock_limit = "30m"
order = 40
""",
    )
    write(
        c / "environments" / "client.toml",
        """
workspace = "~/work/client"
effort_default = "deep-review"
[privacy]
vault = "digests"
""",
    )
    write(
        c / "clients.toml",
        """
[clients.open-webui]
kind = "chat"
profile = "open-webui"
default_environment = "client"
[clients.aider]
kind = "agent"
profile = "aider"
""",
    )
    cfg = load_config(paths)
    assert len(cfg.files_loaded) == 5
    assert cfg.effort_levels_ordered() == ["low", "medium", "high", "deep-review"]
    assert cfg.environments["client"].workspace[0] == Path("~/work/client").expanduser()
    eff = cfg.privacy("client")
    assert eff.value("approvals") == "high_only"
    assert eff.value("vault") == "digests"
    assert eff.label == "Strict (custom: approvals, vault)"


def test_all_problems_reported_together(paths: Paths) -> None:
    c = paths.config_dir
    write(
        c / "cairnward.toml",
        """
[server]
dashboard_host = "0.0.0.0"
[privacy]
netwrok = "off"
[defaults]
environment = "nope"
""",
    )
    write(
        c / "backends.toml",
        """
[backends.cloud]
driver = "openai_compat"
url = "http://api.example.com/v1"
location = "hosted"
api_key = "sk-live-123"
""",
    )
    write(c / "environments" / "Bad Name.toml", "")
    with pytest.raises(ConfigError) as exc:
        load_config(paths)
    text = "\n".join(exc.value.problems)
    assert "dashboard_host" in text and "not a loopback address" in text
    assert "privacy.netwrok: unknown setting" in text
    assert "keys are never stored in config files" in text
    # (The https rule for this backend is checked once its api_key is valid; see
    # test_backend_url_must_match_location.)
    assert "environment file name" in text
    # cairnward.toml failed validation, so its defaults apply and 'nope' isn't cross-checked.
    assert "unknown environment 'nope'" not in text


def test_invalid_toml_reported_with_file(paths: Paths) -> None:
    write(paths.config_dir / "effort.toml", "[effort.low\n")
    with pytest.raises(ConfigError) as exc:
        load_config(paths)
    assert "effort.toml: not valid TOML" in exc.value.problems[0]


def test_cross_file_references(paths: Paths) -> None:
    write(
        paths.config_dir / "environments" / "x.toml",
        'backend = "missing"\neffort_default = "nope"\n',
    )
    write(
        paths.config_dir / "clients.toml",
        '[clients.c]\nkind = "agent"\nprofile = "aider"\ndefault_environment = "ghost"\n',
    )
    with pytest.raises(ConfigError) as exc:
        load_config(paths)
    text = "\n".join(exc.value.problems)
    assert "unknown backend 'missing'" in text
    assert "unknown effort level 'nope'" in text
    assert "unknown environment 'ghost'" in text


def test_environment_file_replaces_builtin(paths: Paths) -> None:
    write(paths.config_dir / "environments" / "scratch.toml", '[privacy]\npreset = "strict"\n')
    assert load_config(paths).privacy("scratch").label == "Strict"


@pytest.mark.parametrize(
    ("location", "url", "ok"),
    [
        ("local", "http://127.0.0.1:11434", True),
        ("local", "http://10.0.0.5:11434", False),
        ("lan", "http://10.0.0.5:8000/v1", True),
        ("lan", "http://8.8.8.8:8000/v1", False),
        ("lan", "http://gpu-box.lan:8000/v1", True),
        ("hosted", "https://openrouter.ai/api/v1", True),
        ("hosted", "http://openrouter.ai/api/v1", False),
    ],
)
def test_backend_url_must_match_location(location: str, url: str, ok: bool) -> None:
    data = {"driver": "openai_compat", "url": url, "location": location}
    if ok:
        Backend.model_validate(data)
    else:
        with pytest.raises(ValueError):
            Backend.model_validate(data)


def test_fallback_only_local(paths: Paths) -> None:
    write(
        paths.config_dir / "backends.toml",
        """
fallback = ["cloud"]
[backends.local-ollama]
driver = "ollama"
url = "http://127.0.0.1:11434"
[backends.cloud]
driver = "openai_compat"
url = "https://openrouter.ai/api/v1"
location = "hosted"
""",
    )
    with pytest.raises(ConfigError, match="fallback only uses local backends"):
        load_config(paths)


def test_server_ports_and_loopback() -> None:
    with pytest.raises(ValueError):
        ServerConfig(gateway_port=9000, dashboard_port=9000)
    with pytest.raises(ValueError):
        ServerConfig(egress_port=8484)
    assert ServerConfig(dashboard_host="::1").dashboard_host == "::1"


def test_effort_needs_model_or_role(paths: Paths) -> None:
    write(paths.config_dir / "effort.toml", "[effort.bad]\nthinking = true\n")
    with pytest.raises(ConfigError, match="exactly one of model_role or model"):
        load_config(paths)


def test_parse_duration() -> None:
    assert parse_duration("90s") == 90
    assert parse_duration("30m") == 1800
    assert parse_duration("2h") == 7200
    with pytest.raises(ValueError):
        parse_duration("soon")


def test_example_configs_load(tmp_path: Path) -> None:
    """examples/config must stay valid."""
    import shutil

    example = Path(__file__).resolve().parents[1] / "examples" / "config"
    cfg_dir = tmp_path / "cfg"
    shutil.copytree(example, cfg_dir)
    cfg = load_config(Paths(cfg_dir, tmp_path / "d", tmp_path / "s"))
    assert "deep-review" in cfg.effort
    assert cfg.privacy("confidential").label == "Strict (custom: vault)"
    assert set(cfg.clients.clients) == {"open-webui", "aider"}
