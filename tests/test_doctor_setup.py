import tomllib
from pathlib import Path

import pytest

from cairnward.config import load_config
from cairnward.config.paths import Paths
from cairnward.doctor import _memory_check, check_isolation, run_checks
from cairnward.setup import choose, set_preset, setup


@pytest.fixture
def paths(tmp_path: Path) -> Paths:
    return Paths(tmp_path / "cfg", tmp_path / "data", tmp_path / "state")


def test_set_preset_new_file(paths: Paths) -> None:
    set_preset(paths.main_config, "strict")
    assert tomllib.loads(paths.main_config.read_text())["privacy"]["preset"] == "strict"
    assert load_config(paths).privacy().label == "Strict"


def test_set_preset_keeps_other_settings(paths: Paths) -> None:
    paths.config_dir.mkdir(parents=True)
    paths.main_config.write_text(
        '[server]\ngateway_port = 9000\n\n[privacy]\napprovals = "tiered"\npreset = "open"\n'
        '\n[defaults]\neffort = "low"\n'
    )
    set_preset(paths.main_config, "balanced")
    data = tomllib.loads(paths.main_config.read_text())
    assert data["privacy"] == {"approvals": "tiered", "preset": "balanced"}
    assert data["server"]["gateway_port"] == 9000 and data["defaults"]["effort"] == "low"


def test_set_preset_adds_missing_section(paths: Paths) -> None:
    paths.config_dir.mkdir(parents=True)
    paths.main_config.write_text("[server]\ngateway_port = 9000\n")
    set_preset(paths.main_config, "open")
    assert tomllib.loads(paths.main_config.read_text())["privacy"]["preset"] == "open"
    with pytest.raises(ValueError):
        set_preset(paths.main_config, "paranoid")


def test_choose() -> None:
    said: list[str] = []
    replies = iter(["9", "strict"])
    assert choose(lambda _: next(replies), said.append) == "strict"
    assert any("Please answer" in s for s in said)
    assert choose(lambda _: "", said.append) == "balanced"
    assert choose(lambda _: "1", said.append) == "open"


def test_doctor_reports_unreachable_backend(paths: Paths) -> None:
    paths.config_dir.mkdir(parents=True)
    (paths.config_dir / "backends.toml").write_text(
        '[backends.local-ollama]\ndriver = "ollama"\nurl = "http://127.0.0.1:9"\n'
        '[roles.main]\nbackend = "local-ollama"\nmodel = "m"\n'
    )
    (paths.config_dir / "cairnward.toml").write_text('[privacy]\npreset = "open"\n')
    checks = {c.name: c for c in run_checks(paths)}
    assert checks["configuration"].status == "ok"
    assert checks["backend local-ollama"].status == "fail"
    assert "start the model server" in checks["backend local-ollama"].fix
    assert checks["isolation"].status == "skip"  # open preset: isolation off everywhere


def test_doctor_reports_bad_config(paths: Paths) -> None:
    paths.config_dir.mkdir(parents=True)
    paths.main_config.write_text('[server]\ngateway_host = "0.0.0.0"\n')
    checks = run_checks(paths)
    assert checks[-1].name == "configuration" and checks[-1].status == "fail"


def test_memory_estimate(paths: Paths, monkeypatch: pytest.MonkeyPatch) -> None:
    cfg = load_config(paths)
    gb = 1024**3
    monkeypatch.setattr(
        "cairnward.doctor._meminfo", lambda: {"MemTotal": 29 * gb, "MemAvailable": 9 * gb}
    )
    sizes = {"qwen3:30b": 18 * gb, "qwen3:1.7b": int(1.4 * gb)}
    assert _memory_check(cfg, sizes).status == "warn"  # both don't fit in 9 GB available
    monkeypatch.setattr(
        "cairnward.doctor._meminfo", lambda: {"MemTotal": 16 * gb, "MemAvailable": 15 * gb}
    )
    assert _memory_check(cfg, sizes).status == "fail"  # main model alone too big
    monkeypatch.setattr(
        "cairnward.doctor._meminfo", lambda: {"MemTotal": 64 * gb, "MemAvailable": 50 * gb}
    )
    assert _memory_check(cfg, sizes).status == "ok"


def test_isolation_checks_when_requested(paths: Paths, monkeypatch: pytest.MonkeyPatch) -> None:
    cfg = load_config(paths)  # balanced: isolation on
    monkeypatch.setattr("cairnward.doctor.shutil.which", lambda _tool: None)
    monkeypatch.setattr("cairnward.doctor.locate", lambda _p: None)
    monkeypatch.setattr("cairnward.doctor.node_version", lambda: (20, 19))
    monkeypatch.setattr("cairnward.doctor.sys.platform", "linux")
    checks = {c.name: c for c in check_isolation(cfg)}
    assert checks["bwrap"].status == "fail" and "bubblewrap" in checks["bwrap"].fix
    assert checks["node"].status == "fail" and checks["sandbox-runtime"].status == "fail"
    assert "isolation self-test" not in checks  # not attempted without prerequisites


def test_setup_warns_about_the_backup_password(paths: Paths) -> None:
    said: list[str] = []
    setup(paths, preset="balanced", ask=None, say=said.append)
    assert any("Keep a copy somewhere else" in line for line in said)
