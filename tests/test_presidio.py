"""Presidio in the redaction scanner (FR-RED-2): the real worker script with a stand-in
`presidio_analyzer` package (the real one needs spaCy's model, which the build
environment can't download): findings merged with the patterns' (patterns win on
overlaps), a dead worker leaves the patterns working and says so, then restarts."""

import subprocess
import sys
from pathlib import Path

import pytest

from cairnward.config.models import RedactionConfig
from cairnward.config.paths import Paths
from cairnward.redaction import Scanner, scan_texts
from cairnward.research.engines import presidio

FAKE = """
import os


class _R:
    def __init__(self, t, s, e, score):
        self.entity_type, self.start, self.end, self.score = t, s, e, score


class AnalyzerEngine:
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
