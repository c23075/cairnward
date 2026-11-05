"""Presidio in the redaction scanner (FR-RED-2): the real worker script with a stand-in
`presidio_analyzer` package (the real one needs spaCy's model, which the build
environment can't download): findings merged with the patterns' (patterns win on
overlaps), a dead worker leaves the patterns working and says so, then restarts."""

import json
import shutil
import subprocess
import sys
from pathlib import Path
from typing import Any

import pytest

from cairnward.config.models import RedactionConfig
from cairnward.config.paths import Paths
from cairnward.redaction import Scanner, scan_texts
from cairnward.redaction_engines import presidio

FAKE = """
import os


class _R:
    def __init__(self, t, s, e, score):
        self.entity_type, self.start, self.end, self.score = t, s, e, score


class AnalyzerEngine:
    def __init__(self, nlp_engine, supported_languages):
        assert nlp_engine.model == "en_core_web_sm" and supported_languages == ["en"]

    def analyze(self, text, language, entities=None):
        if "CRASH" in text:
            os._exit(3)
        out = []
        for word, kind, score in (("Lovelace", "PERSON", 0.85), ("London", "LOCATION", 0.9),
                                  ("Ada", "PERSON", 0.3)):
            i = text.find(word)
            if i >= 0 and (not entities or kind in entities):
                out.append(_R(kind, i, i + len(word), score))
        return out
"""


FAKE_NLP = """
class _Nlp:
    def __init__(self, model):
        self.model = model


class NlpEngineProvider:
    def __init__(self, nlp_configuration):
        self.cfg = nlp_configuration

    def create_engine(self):
        return _Nlp(self.cfg["models"][0]["model_name"])
"""


@pytest.fixture
def paths(tmp_path: Path) -> Paths:
    if sys.platform == "win32":
        pytest.skip("POSIX venv layout")
    p = Paths(tmp_path / "cfg", tmp_path / "data", tmp_path / "state")
    venv = presidio.engine_dir(p) / "venv"
    subprocess.run([sys.executable, "-m", "venv", "--without-pip", str(venv)], check=True)
    site = next((venv / "lib").glob("python*/site-packages"))
    (site / "presidio_analyzer").mkdir()
    (site / "presidio_analyzer" / "__init__.py").write_text(FAKE)
    (site / "presidio_analyzer" / "nlp_engine.py").write_text(FAKE_NLP)
    return p


def test_presidio_findings_join_the_patterns(paths: Paths) -> None:
    cfg = RedactionConfig(presidio=True)
    scanner = Scanner.from_config(cfg, paths)
    try:
        text = "Write to Lovelace at lovelace.ada@example.org about London."
        found = {(f.kind, text[f.start : f.end]) for f in scanner.scan(text)}
        assert ("presidio:person", "Lovelace") in found
        assert ("presidio:location", "London") in found
        assert ("email", "lovelace.ada@example.org") in found  # the pattern wins the overlap
        assert not any(k == "presidio:person" and v == "Ada" for k, v in found)  # below 0.6
        # the worker dies: patterns still work, the report says Presidio was unavailable
        report = scan_texts(scanner, ["CRASH, mail me at x@example.org"])
        assert report.personal["email"] == 1 and "presidio" in report.unavailable[0]
        # and it comes back on the next scan
        again = Scanner.from_config(cfg, paths)
        assert any(f.kind == "presidio:location" for f in again.scan("from London"))
    finally:
        presidio.shared(paths).close()


def test_off_by_default_and_without_paths(paths: Paths) -> None:
    assert Scanner.from_config(RedactionConfig(), paths).presidio is None
    assert Scanner.from_config(RedactionConfig(presidio=True)).presidio is None


def _stand_in_worker(monkeypatch: pytest.MonkeyPatch, tmp_path: Path, body: str) -> None:
    script = tmp_path / "worker.py"
    script.write_text(body)
    monkeypatch.setattr(presidio, "WORKER", script)


def test_not_installed_is_said_plainly(tmp_path: Path) -> None:
    empty = Paths(tmp_path / "c", tmp_path / "d", tmp_path / "s")
    with pytest.raises(presidio.PresidioError, match="isn't installed"):
        presidio.Presidio(empty).analyze("text", [], 0.5)


@pytest.mark.parametrize(
    ("body", "said"),
    [
        ("print('Traceback', flush=True)", "didn't start"),
        ("print('READY', flush=True)\ninput()\nprint('not json', flush=True)", "nonsense"),
        ("print('READY', flush=True)\ninput()\nprint('{\"error\": \"bad\"}', flush=True)", "bad"),
    ],
)
def test_a_worker_that_misbehaves_is_an_error(paths: Paths, tmp_path: Path, body: str, said: str,
                                              monkeypatch: pytest.MonkeyPatch) -> None:  # fmt: skip
    _stand_in_worker(monkeypatch, tmp_path, body)
    engine = presidio.Presidio(paths)
    try:
        with pytest.raises(presidio.PresidioError, match=said):
            engine.analyze("text", [], 0.5)
    finally:
        engine.close()


def test_a_worker_that_hangs_is_stopped(paths: Paths, tmp_path: Path,
                                        monkeypatch: pytest.MonkeyPatch) -> None:  # fmt: skip
    _stand_in_worker(monkeypatch, tmp_path, "print('READY', flush=True)\ninput()\ninput()")
    monkeypatch.setattr(presidio, "REQUEST_TIMEOUT_S", 0.2)
    engine = presidio.Presidio(paths)
    try:
        with pytest.raises(presidio.PresidioError, match="stopped answering"):
            engine.analyze("text", [], 0.5)
    finally:
        engine.close()


def _fake_uv(monkeypatch: pytest.MonkeyPatch, paths: Paths, *, venv_made: bool = True) -> list[Any]:
    """What `install` asks uv to do, without downloading anything."""
    calls: list[Any] = []

    def run(cmd: list[str], **_: Any) -> subprocess.CompletedProcess[str]:
        calls.append(cmd)
        if cmd[1:3] == ["pip", "compile"]:
            Path(cmd[cmd.index("-o") + 1]).write_text("resolved\n")
        if cmd[1] == "venv" and venv_made:
            exe = Path(cmd[-1]) / (
                "Scripts/python.exe" if sys.platform == "win32" else "bin/python"
            )
            exe.parent.mkdir(parents=True)
            exe.write_text("")
        return subprocess.CompletedProcess(cmd, 0, stdout="presidio-analyzer==1\n")

    monkeypatch.setattr(shutil, "which", lambda _: "uv")
    monkeypatch.setattr(subprocess, "run", run)
    return calls


def _empty(tmp_path: Path) -> Paths:
    return Paths(tmp_path / "c", tmp_path / "d", tmp_path / "s")


def test_install_uses_the_shipped_lock_and_requires_hashes(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    p = _empty(tmp_path)
    calls = _fake_uv(monkeypatch, p)
    record = presidio.install(p, log=lambda _: None)
    assert record["lock_source"].startswith("repository")
    assert not any(c[1:3] == ["pip", "compile"] for c in calls)
    assert any("--require-hashes" in c for c in calls)
    assert json.loads((presidio.engine_dir(p) / "install.json").read_text()) == record


def test_install_without_a_shipped_lock_resolves_one(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from cairnward.redaction_engines import locks

    p = _empty(tmp_path)
    monkeypatch.setattr(locks, "shipped", lambda _: None)
    calls = _fake_uv(monkeypatch, p)
    record = presidio.install(p, log=lambda _: None)
    assert record["lock_source"] == "resolved on this machine"
    assert any(c[1:3] == ["pip", "compile"] and "--generate-hashes" in c for c in calls)


def test_install_says_what_went_wrong(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    p = _empty(tmp_path)
    _fake_uv(monkeypatch, p, venv_made=False)
    with pytest.raises(presidio.PresidioError, match="wasn't created"):
        presidio.install(p, log=lambda _: None)
    monkeypatch.setattr(shutil, "which", lambda _: None)
    with pytest.raises(presidio.PresidioError, match="needs uv"):
        presidio.install(p, log=lambda _: None)
