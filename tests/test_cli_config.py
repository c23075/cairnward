import json
from pathlib import Path

import pytest

from cairnward.cli import main
from cairnward.config.schema import render, schemas

REPO = Path(__file__).resolve().parents[1]


def test_config_check_defaults(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    assert main(["--config-dir", str(tmp_path), "config", "check"]) == 0
    out = capsys.readouterr().out
    assert "Configuration OK" in out and "qwen3:30b" in out and "Balanced" in out


def test_config_check_reports_problems(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    (tmp_path / "cairnward.toml").write_text('[server]\ngateway_host = "0.0.0.0"\n')
    assert main(["--config-dir", str(tmp_path), "config", "check"]) == 1
    assert "not a loopback address" in capsys.readouterr().err


def test_privacy_show_env(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    assert main(["--config-dir", str(tmp_path), "privacy", "show", "--env", "scratch"]) == 0
    out = capsys.readouterr().out
    assert "Privacy for environment 'scratch': Open" in out
    assert "weaker than Strict" in out


def test_privacy_show_json_and_all(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    assert main(["--config-dir", str(tmp_path), "privacy", "show", "--all", "--json"]) == 0
    rows = json.loads(capsys.readouterr().out)
    assert rows[0]["environment"] is None and rows[0]["label"] == "Balanced"
    names = [r["environment"] for r in rows[1:]]
    assert names == sorted(names)


def test_privacy_show_unknown_env(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    assert main(["--config-dir", str(tmp_path), "privacy", "show", "--env", "nope"]) == 2
    assert "Unknown environment" in capsys.readouterr().err


def test_committed_schemas_are_up_to_date() -> None:
    """Contract test: run `cairnward config schema` after changing a config model."""
    for filename, schema in schemas().items():
        path = REPO / "schemas" / filename
        assert path.exists(), f"missing {path}"
        assert path.read_text(encoding="utf-8") == render(schema), f"{filename} is stale"
