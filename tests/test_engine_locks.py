"""Engine locks shipped in the repository: Presidio's covers every package and the spaCy
model wheel with hashes; a shipped lock is preferred over resolving; a lock recorded on
a machine can be exported for committing."""

import re
from pathlib import Path

import pytest

from cairnward.redaction_engines import locks, presidio


def requirements(text: str) -> list[str]:
    """Each requirement with its continuation lines joined."""
    return [r.strip() for r in re.sub(r"\\\n\s*", " ", text).splitlines()
            if r.strip() and not r.lstrip().startswith("#")]  # fmt: skip


def test_presidio_lock_is_complete() -> None:
    lock = locks.shipped("presidio")
    assert lock is not None and lock.name == "presidio.txt"
    reqs = requirements(lock.read_text(encoding="utf-8"))
    assert all("--hash=sha256:" in r for r in reqs)  # installable with --require-hashes
    assert any(r.startswith(presidio.PIN + " ") for r in reqs)
    model = next(r for r in reqs if r.startswith("en-core-web-sm @ "))
    assert presidio.SPACY_MODEL_URL in model
    version = re.search(r"en_core_web_sm-([0-9.]+)-py3", presidio.SPACY_MODEL_URL)[1]  # type: ignore[index]
    spacy = next(r for r in reqs if r.startswith("spacy=="))
    assert spacy.split("==")[1].split(".")[:2] == version.split(".")[:2]  # model fits spaCy


def test_shipped_and_export(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(locks, "LOCK_DIR", tmp_path / "locks")
    assert locks.shipped("other-engine") is None
    recorded = tmp_path / "requirements.lock.txt"
    with pytest.raises(FileNotFoundError):
        locks.export("other-engine", recorded)
    recorded.write_text("other-engine==2.140.0 --hash=sha256:00\n")
    dest = locks.export("other-engine", recorded)
    assert dest.name == f"other-engine-{locks.platform_tag()}.txt"
    assert locks.shipped("other-engine") == dest
    (tmp_path / "locks" / "other-engine.txt").write_text("x")
    assert locks.shipped("other-engine") == dest  # a platform-specific lock comes first
